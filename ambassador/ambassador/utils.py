#!/usr/bin/env python

# Copyright 2018 Datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License

from typing import Dict, Optional, TYPE_CHECKING

import binascii
import socket
import threading
import time
import os
import logging
import yaml

from kubernetes import client, config
from enum import Enum

from .VERSION import Version

if TYPE_CHECKING:
    from .ir.irtlscontext import IRTLSContext

logger = logging.getLogger("utils")
logger.setLevel(logging.INFO)


class TLSPaths(Enum):
    mount_cert_dir = "/etc/certs"
    mount_tls_crt = os.path.join(mount_cert_dir, "tls.crt")
    mount_tls_key = os.path.join(mount_cert_dir, "tls.key")

    client_mount_dir = "/etc/cacert"
    client_mount_crt = os.path.join(client_mount_dir, "tls.crt")

    cert_dir = "/ambassador/certs"
    tls_crt = os.path.join(cert_dir, "tls.crt")
    tls_key = os.path.join(cert_dir, "tls.key")

    client_cert_dir = "/ambassador/cacert"
    client_tls_crt = os.path.join(client_cert_dir, "tls.crt")

    @staticmethod
    def generate(directory):
        return {
            'crt': os.path.join(directory, 'tls.crt'),
            'key': os.path.join(directory, 'tls.key')
        }

class SystemInfo:
    MyHostName = 'localhost'
    MyResolvedName = '127.0.0.1'

    try:
        MyHostName = socket.gethostname()
        MyResolvedName = socket.gethostbyname(socket.gethostname())
    except:
        pass

class RichStatus:
    def __init__(self, ok, **kwargs):
        self.ok = ok
        self.info = kwargs
        self.info['hostname'] = SystemInfo.MyHostName
        self.info['resolvedname'] = SystemInfo.MyResolvedName
        self.info['version'] = Version

    # Remember that __getattr__ is called only as a last resort if the key
    # isn't a normal attr.
    def __getattr__(self, key):
        return self.info.get(key)

    def __bool__(self):
        return self.ok

    def __nonzero__(self):
        return bool(self)
        
    def __contains__(self, key):
        return key in self.info

    def __str__(self):
        attrs = ["%s=%s" % (key, self.info[key]) for key in sorted(self.info.keys())]
        astr = " ".join(attrs)

        if astr:
            astr = " " + astr

        return "<RichStatus %s%s>" % ("OK" if self else "BAD", astr)

    def as_dict(self):
        d = { 'ok': self.ok }

        for key in self.info.keys():
            d[key] = self.info[key]

        return d

    @classmethod
    def fromError(self, error, **kwargs):
        kwargs['error'] = error
        return RichStatus(False, **kwargs)

    @classmethod
    def OK(self, **kwargs):
        return RichStatus(True, **kwargs)

class SourcedDict (dict):
    def __init__(self, _source="--internal--", _from=None, **kwargs):
        super().__init__(self, **kwargs)

        if _from and ('_source' in _from):
            self['_source'] = _from['_source']
        else:
            self['_source'] = _source

        # self['_referenced_by'] = []

    def referenced_by(self, source):
        refby = self.setdefault('_referenced_by', [])

        if source not in refby:
            refby.append(source)

class DelayTrigger (threading.Thread):
    def __init__(self, onfired, timeout=5, name=None):
        super().__init__()

        if name:
            self.name = name

        self.trigger_source, self.trigger_dest = socket.socketpair()

        self.onfired = onfired
        self.timeout = timeout

        self.setDaemon(True)
        self.start()

    def trigger(self):
        self.trigger_source.sendall(b'X')

    def run(self):
        while True:
            self.trigger_dest.settimeout(None)
            x = self.trigger_dest.recv(128)

            self.trigger_dest.settimeout(self.timeout)

            while True:
                try:
                    x = self.trigger_dest.recv(128)
                except socket.timeout:
                    self.onfired()
                    break


class PeriodicTrigger(threading.Thread):
    def __init__(self, onfired, period=5, name=None):
        super().__init__()

        if name:
            self.name = name

        self.onfired = onfired
        self.period = period

        self.daemon = True
        self.start()

    def trigger(self):
        pass

    def run(self):
        while True:
            time.sleep(self.period)
            self.onfired()


class SavedSecret:
    def __init__(self, secret_name: str, namespace: str,
                 cert_path: Optional[str], key_path: Optional[str], cert_data: Optional[Dict]) -> None:
        self.secret_name = secret_name
        self.namespace = namespace
        self.cert_path = cert_path
        self.key_path = key_path
        self.cert_data = cert_data

    @property
    def name(self) -> str:
        return "secret %s in namespace %s" % (self.secret_name, self.namespace)

    def __bool__(self) -> bool:
        return bool(bool(self.cert_path) and (self.cert_data is not None))

    def __str__(self) -> str:
        return "<SavedSecret %s.%s -- cert_path %s, key_path %s, cert_data %s>" % (
                  self.secret_name, self.namespace, self.cert_path, self.key_path,
                  "present" if self.cert_data else "absent"
                )


class KubeSecretReader:
    def __init__(self) -> None:
        self.v1 = None
        self.__name__ = 'KubeSecretReader'

    def __call__(self, context: 'IRTLSContext', secret_name: str, namespace: str, secret_root: str):
        # Make sure we have a Kube connection.
        if not self.v1:
            self.v1 = kube_v1()

        cert_data = None
        cert = None
        key = None

        if self.v1:
            try:
                cert_data = self.v1.read_namespaced_secret(secret_name, namespace)
            except client.rest.ApiException as e:
                if e.reason == "Not Found":
                    logger.info("secret {} not found".format(secret_name))
                else:
                    logger.info("secret %s/%s could not be read: %s" % (namespace, secret_name, e))

        if cert_data and cert_data.data:
            cert_data = cert_data.data
            cert = cert_data.get('tls.crt', None)

            if cert:
                cert = binascii.a2b_base64(cert)

            key = cert_data.get('tls.key', None)

            if key:
                key = binascii.a2b_base64(key)

        secret_dir = os.path.join(secret_root, namespace, "secrets", secret_name)

        cert_path = None
        key_path = None

        if cert:
            try:
                os.makedirs(secret_dir)
            except FileExistsError:
                pass

            cert_path = os.path.join(secret_dir, "tls.crt")
            open(cert_path, "w").write(cert.decode("utf-8"))

            if key:
                key_path = os.path.join(secret_dir, "tls.key")
                open(key_path, "w").write(key.decode("utf-8"))

        return SavedSecret(secret_name, namespace, cert_path, key_path, cert_data)


class SplitConfigChecker:
    def __init__(self, logger, root_path: str) -> None:
        self.logger = logger
        self.root = root_path

    def secret_reader(self, context: 'IRTLSContext', secret_name: str, namespace: str, secret_root: str):
        yaml_path = os.path.join(self.root, namespace, "secrets", "%s.yaml" % secret_name)

        serialization = None
        objects = []
        cert_data = None
        cert = None
        key = None
        cert_path = None
        key_path = None

        try:
            serialization = open(yaml_path, "r").read()
        except IOError as e:
            self.logger.error("TLSContext %s: SCC.secret_reader could not open %s" % (context.name, yaml_path))

        if serialization:
            try:
                objects.extend(list(yaml.safe_load_all(serialization)))
            except yaml.error.YAMLError as e:
                self.logger.error("TLSContext %s: SCC.secret_reader could not parse %s: %s" %
                                  (context.name, yaml_path, e))

        ocount = 0
        errors = 0

        for obj in objects:
            ocount += 1
            kind = obj.get('kind', None)

            if kind != "Secret":
                self.logger.error("TLSContext %s: SCC.secret_reader found K8s %s at %s.%d?" %
                                  (context.name, kind, yaml_path, ocount))
                errors += 1
                continue

            metadata = obj.get('metadata', None)

            if not metadata:
                self.logger.error("TLSContext %s: SCC.secret_reader found K8s Secret with no metadata at %s.%d?" %
                                  (context.name, yaml_path, ocount))
                errors += 1
                continue

            if 'data' in obj:
                if cert_data:
                    self.logger.error("TLSContext %s: SCC.secret_reader found multiple Secrets in %s?" %
                                      (context.name, yaml_path))
                    errors += 1
                    continue

                cert_data = obj['data']

        # if errors:
        #     return None
        #
        # if not cert_data:
        #     self.logger.error("TLSContext %s: SCC.secret_reader found no certificate in %s?" %
        #                       (context.name, yaml_path))
        #     return None

        # OK, we have something to work with. Hopefully.
        if not errors and cert_data:
            cert = cert_data.get('tls.crt', None)

            if cert:
                cert = binascii.a2b_base64(cert)

            key = cert_data.get('tls.key', None)

            if key:
                key = binascii.a2b_base64(key)

        # if not cert:
        #     # This is an error. Having a cert but no key might be OK, we'll let our caller decide.
        #     self.logger.error("TLSContext %s: SCC.secret_reader found data but no cert in %s?" %
        #                       (context.name, yaml_path))
        #     return None

        if cert:
            secret_dir = os.path.join(self.root, namespace, "secrets-decoded", secret_name)

            try:
                os.makedirs(secret_dir)
            except FileExistsError:
                pass

            cert_path = os.path.join(secret_dir, "tls.crt")
            open(cert_path, "w").write(cert.decode("utf-8"))

            if key:
                key_path = os.path.join(secret_dir, "tls.key")
                open(key_path, "w").write(key.decode("utf-8"))

        return SavedSecret(secret_name, namespace, cert_path, key_path, cert_data)


def kube_v1():
    # Assume we got nothin'.
    k8s_api = None

    # XXX: is there a better way to check if we are inside a cluster or not?
    if "KUBERNETES_SERVICE_HOST" in os.environ:
        # If this goes horribly wrong and raises an exception (it shouldn't),
        # we'll crash, and Kubernetes will kill the pod. That's probably not an
        # unreasonable response.
        config.load_incluster_config()
        if "AMBASSADOR_VERIFY_SSL_FALSE" in os.environ:
            configuration = client.Configuration()
            configuration.verify_ssl=False
            client.Configuration.set_default(configuration)
        k8s_api = client.CoreV1Api()
    else:
        # Here, we might be running in docker, in which case we'll likely not
        # have any Kube secrets, and that's OK.
        try:
            config.load_kube_config()
            k8s_api = client.CoreV1Api()
        except FileNotFoundError:
            # Meh, just ride through.
            logger.info("No K8s")
            pass

    return k8s_api


def check_cert_file(path):
    readable = False

    try:
        data = open(path, "r").read()

        if data and (len(data) > 0):
            readable = True
    except OSError:
        pass
    except IOError:
        pass

    return readable
