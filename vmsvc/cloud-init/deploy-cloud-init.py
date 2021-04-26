#!env python3

import time
import argparse
import logging
import json
import tempfile
import uuid
import os
import shutil
import zipfile
import pexpect
import string
import yaml
import re
import base64

import requests
import kubernetes
import urllib3

urllib3.disable_warnings()

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


VM_MANIFEST = """\
apiVersion: vmoperator.vmware.com/v1alpha1
kind: VirtualMachine
metadata:
  name: ${deployment_name}
  namespace: ${deployment_namespace}
  annotations:
    vmoperator.vmware.com/vm-customize: disable
spec:
  className: best-effort-medium
  imageName: ${deployment_image}
  powerState: poweredOn
  storageClass: wcpglobal-storage-profile
  vmMetadata:
    configMapName: ${deployment_name}
    transport: OvfEnv
"""


VM_METADATA_CONFIGMAP = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${deployment_name}
  namespace: ${deployment_namespace}
  annotations:
    vmoperator.vmware.com/ovfenv-transport: com.vmware.guestinfo,iso
data:
  user-data: ""
"""


class VCenter(object):
    def __init__(self, vc, username, password, namespace):
        self.vc = vc
        self.session = requests.sessions.Session()
        self.session.auth = (username, password)
        self.session.verify = False
        self.session.headers.update({"Content-Type": "application/json"})
        self.login()

    def url(self, path):
        return "https://%s/%s" % (self.vc, path.lstrip("/"))

    def login(self):
        """Authenticate with vCenter and populate the cookiejar."""
        res = self.session.post(self.url("/rest/com/vmware/cis/session"))
        res.raise_for_status()

    def get_datastores(self):
        """Get all available datastores."""
        res = self.session.get(self.url("rest/vcenter/datastore"))
        res.raise_for_status()
        return res.json()["value"]

    def get_content_libraries(self):
        res = self.session.get(self.url("/rest/com/vmware/content/subscribed-library"))
        res.raise_for_status()
        libraries = []
        for library_id in res.json()["value"]:
            res = self.session.get(
                self.url(
                    "/rest/com/vmware/content/subscribed-library/id:%s" % library_id
                )
            )
            res.raise_for_status()
            libraries.append(res.json()["value"])
        return libraries

    def get_content_library_by_subscription(self, url):
        for cl in self.get_content_libraries():
            if cl.get("subscription_info", {}).get("subscription_url") == url:
                return cl

    def get_content_library_by_name(self, name):
        for cl in self.get_content_libraries():
            if cl.get("name") == name:
                return cl

    def create_subscribed_content_library(self, url, datastore, name):
        spec = {
            "client_token": str(uuid.uuid4()),
            "create_spec": {
                "name": name,
                "description": "",
                "type": "SUBSCRIBED",
                "subscription_info": {
                    "subscription_url": url,
                    #"ssl_fingerprint": "C0:96:A6:48:9B:50:7D:08:0B:63:59:6B:52:69:85:69:68:7D:10:83",
                    "authentication_method": "NONE",
                    "automatic_sync_enabled": True,
                    "on_demand": True,
                },
                "storage_backings": [{"datastore_id": datastore, "type": "DATASTORE"}],
            },
        }
        res = self.session.post(
            self.url("/rest/com/vmware/content/subscribed-library"),
            data=json.dumps(spec),
        )
        res.raise_for_status()
        res.close()
        return vc.get_content_library_by_name(name)

    def get_cluster(self, cluster_id):
        res = self.session.get(
            self.url("/api/vcenter/namespace-management/clusters/%s" % cluster_id)
        )
        res.raise_for_status()
        return res.json()

    def ensure_cluster_has_content_library(self, cluster_id, cl_id):
        res = self.session.get(
            self.url("/api/vcenter/namespace-management/clusters/%s" % cluster_id)
        )
        res.raise_for_status()
        if res.json().get("default_kubernetes_service_content_library") == cl_id:
            return

        spec = {"default_kubernetes_service_content_library": cl_id}
        res = self.session.patch(
            self.url("/api/vcenter/namespace-management/clusters/%s" % cluster_id),
            data=json.dumps(spec),
        )
        res.raise_for_status()

    def get_namespace(self, namespace_id):
        res = self.session.get(
            self.url("/api/vcenter/namespaces/instances/%s" % namespace_id)
        )
        res.raise_for_status()
        return res.json()

    def ensure_namespace_has_access(self, namespace_id, username):
        username, _, domain = username.partition("@")
        res = self.session.get(
            self.url(
                "/api/vcenter/namespaces/instances/%s/access/%s/%s?type=USER"
                % (namespace_id, domain, username)
            )
        )
        res.status_code == 400 or res.raise_for_status()

        spec = {"role": "EDIT"}
        if res.status_code == 400:
            res = self.session.post(
                self.url(
                    "/api/vcenter/namespaces/instances/%s/access/%s/%s?type=USER"
                    % (namespace_id, domain, username)
                ),
                data=json.dumps(spec),
            )
            res.raise_for_status()
        elif res.json()["role"] != "EDIT":
            res = self.session.put(
                self.url(
                    "/api/vcenter/namespaces/instances/%s/access/%s/%s?type=USER"
                    % (namespace_id, domain, username)
                ),
                data=json.dumps(spec),
            )
            res.raise_for_status()

    def ensure_namespace_has_storage_policy(self, namespace_id, storage_policy):
        for sp in self.get_storage_policies():
            if sp["name"] == storage_policy:
                break
        else:
            raise Exception("Storage policy %s not found" % storage_policy)

        namespace = self.get_namespace(namespace_id)
        storage_policies = namespace.get("storage_specs", [])
        for nsp in storage_policies:
            if nsp.get("policy") == sp["policy"]:
                return

        storage_policies.append({"policy": sp["policy"]})
        spec = {"storage_specs": storage_policies}
        res = self.session.patch(
            self.url("/api/vcenter/namespaces/instances/%s" % namespace_id),
            data=json.dumps(spec),
        )
        res.raise_for_status()

    def get_storage_policies(self):
        res = self.session.get(self.url("/rest/vcenter/storage/policies"))
        res.raise_for_status()
        return res.json()["value"]


class KubectlVSphere(object):
    def __init__(self, server, username, password, kubeconfig):
        self.kubeconfig = kubeconfig

        self.temp_dir = tempfile.mkdtemp()
        log.debug("Temp directory for kubectl binaries %s", self.temp_dir)

        self.vsphere_plugin_download(server)
        self.vsphere_login(server, username, password)

        self.kubectl = os.path.join(self.temp_dir, "bin", "kubectl")

    def vsphere_plugin_download(self, server):
        # Download kubectl vsphere plugin and extract
        # TODO: Maybe this can be baked in to the docker image?

        kubectl_path = os.path.join(self.temp_dir, "bin", "kubectl")
        kubectl_vsphere_path = os.path.join(self.temp_dir, "bin", "kubectl-vsphere")

        if os.path.exists(kubectl_path) and os.path.exists(kubectl_vsphere_path):
            return

        if os.uname().sysname == "Darwin":
            plugin_url = (
                "https://%s/wcp/plugin/darwin-amd64/vsphere-plugin.zip" % server
            )
        else:
            plugin_url = "https://%s/wcp/plugin/linux-amd64/vsphere-plugin.zip" % server
        plugin_zipfile = os.path.join(self.temp_dir, "vsphere-plugin.zip")
        with requests.get(plugin_url, verify=False, stream=True) as res:
            res.raise_for_status()
            with open(plugin_zipfile, "wb") as f:
                shutil.copyfileobj(res.raw, f)
        with zipfile.ZipFile(plugin_zipfile) as zip:
            zip.extractall(self.temp_dir)  # Unzips to bin directory

        os.chmod(kubectl_path, 0o777)
        os.chmod(kubectl_vsphere_path, 0o777)

    def vsphere_login(self, server, username, password):
        with pexpect.spawn(
            "kubectl",
            args=[
                "vsphere",
                "login",
                "--insecure-skip-tls-verify",
                "--server",
                server,
                "--vsphere-username",
                username,
            ],
            env={
                "PATH": os.path.join(self.temp_dir, "bin"),
                "KUBECONFIG": self.kubeconfig,
            }
        ) as child:
            child.expect("Password:")
            child.sendline(password)
            child.wait()
        if child.exitstatus:
            raise Exception("Failed to login via kubectl vsphere plugin")


def retry(delay, timeout, raise_on_timeout=True):
    """Helper to be used with for loops to retry stuff."""
    retrylog = logging.getLogger("retry")

    start = time.time()
    elapsed = 0
    while not timeout or elapsed < timeout:
        yield elapsed
        retrylog.debug(
            "Sleeping for %ds, timeout=%ds, elapsed=%ds", delay, timeout, elapsed
        )
        time.sleep(delay)
        elapsed = time.time() - start
        retrylog.debug("Retrying, timeout=%ds, elapsed=%ds", timeout, elapsed)
    if raise_on_timeout:
        raise TimeoutError()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deploy a virtualmachine with a cloud-init vm metadata"
    )

    # vSphere Configuration
    parser.add_argument("--vc", help="vCenter IP or Hostname", required=True)
    parser.add_argument(
        "--vc-username", help="vSphere username", default="Administrator@vsphere.local"
    )
    parser.add_argument("--vc-password", help="vSphere password", default="Admin!23")
    parser.add_argument(
        "--storage-policy",
        help="Storage policy to use for deployment",
        default="wcpglobal_storage_profile",
    )

    # Content Library
    parser.add_argument(
        "--cl-url",
        help="Content library subscription url with the workload image",
        default="http://sc-dbc2110.eng.vmware.com/jkarunaratne/tmp/ubuntu-groovy-20.10-cloudimg--cl/lib.json",
    )
    parser.add_argument(
        "--cl-name",
        help="Content library name that should be used if a new one is to be created",
        default="vmsvc-workload-cl",
    )

    # Workload
    parser.add_argument(
        "--kubeconfig",
        help="Specify a kubeconfig file to configure",
    )
    parser.add_argument(
        "--cloud-config",
        help="Yaml file with the cloud-init configuration",
        required=True,
        type=argparse.FileType('r'),
    )
    parser.add_argument(
        "--namespace",
        help="WCP Namespace to deploy workload",
        default="my-podvm-ns"
    )
    parser.add_argument(
        "--workload-name",
        help="Name to give the deployed workload",
        default="vmsvc-cloud-init-workload-%d" % int(time.time()),
    )
    parser.add_argument(
        "--image-name",
        help="Image name within the content library",
        default="ubuntu-groovy-20.10-cloudimg",
    )
    args = parser.parse_args()

    if args.kubeconfig is None:
        args.kubeconfig = tempfile.mkstemp()[1]
        log.info("Kubeconfig: %s", args.kubeconfig)

    # Connect to vCenter
    vc = VCenter(args.vc, args.vc_username, args.vc_password, args.namespace)

    # Configure namespace
    vc.ensure_namespace_has_storage_policy(args.namespace, args.storage_policy)
    vc.ensure_namespace_has_access(args.namespace, args.vc_username)
    namespace = vc.get_namespace(args.namespace)

    # Configure content library
    cl = vc.get_content_library_by_subscription(args.cl_url)
    if not cl:
        # Pick a NFS datastore
        nfs_datastores = list(filter(lambda d: d["type"] == "NFS", vc.get_datastores()))
        if not nfs_datastores:
            raise Exception("No NFS datastores found")
        # Create the content library with the NFS datastore
        cl = vc.create_subscribed_content_library(
            args.cl_url, nfs_datastores[0]["datastore"], args.cl_name
        )

    # Configure cluster
    vc.ensure_cluster_has_content_library(namespace["cluster"], cl["id"])

    # Get k8s endpoint
    cluster = vc.get_cluster(namespace["cluster"])
    k8s_ip = cluster.get("api_server_management_endpoint")

    # Prepare and login via kubectl vsphere
    kubectl_vsphere = KubectlVSphere(
        k8s_ip, args.vc_username, args.vc_password, args.kubeconfig
    )

    ##########

    # Connect to k8s
    kubernetes.config.load_kube_config(config_file=args.kubeconfig)
    k_v1 = kubernetes.client.CoreV1Api()
    k_ext = kubernetes.client.ExtensionsV1beta1Api()
    k_cust = kubernetes.client.CustomObjectsApi()

    # Prepare manifest
    params = {
        "deployment_namespace": args.namespace,
        "deployment_name": args.workload_name,
        "deployment_image": args.image_name,
    }
    vm_manifest = yaml.safe_load(string.Template(VM_MANIFEST).substitute(params))
    vm_metadata_configmap = yaml.safe_load(string.Template(VM_METADATA_CONFIGMAP).substitute(params))
    vm_metadata_configmap["data"]["user-data"] = base64.b64encode(args.cloud_config.read().encode()).decode()

    print(yaml.dump(vm_manifest))
    print(yaml.dump(vm_metadata_configmap))

    # Apply the manifests
    k_v1.create_namespaced_config_map(
        namespace=args.namespace, body=vm_metadata_configmap
    )
    k_cust.create_namespaced_custom_object(
        group="vmoperator.vmware.com",
        version="v1alpha1",
        namespace=args.namespace,
        plural="virtualmachines",
        body=vm_manifest,
    )

    # Spin until vm has an ip
    for _ in retry(delay=10, timeout=3600):
        obj = k_cust.get_namespaced_custom_object(
            group="vmoperator.vmware.com",
            version="v1alpha1",
            namespace=args.namespace,
            plural="virtualmachines",
            name=vm_manifest["metadata"]["name"],
        )
        vm_ip = obj.get("status", {}).get("vmIp")
        if vm_ip and re.match(r"\d+\.\d+\.\d+\.\d+", vm_ip):
            break

    log.info("Success! Virtual machine ip: %s", vm_ip)
