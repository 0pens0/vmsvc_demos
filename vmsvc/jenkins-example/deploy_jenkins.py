#!env python3

import time
import argparse
import logging
import json
import tempfile
import uuid
import os
import shutil
import sys
import zipfile
import pexpect
import string
import yaml
import socket
import re

import requests
import paramiko
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
spec:
  className: best-effort-xsmall
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
  va-ssh-public-key: "${ssh_pubkey}"
"""

JENKINS_JOB_CONFIG = """\
<?xml version='1.1' encoding='UTF-8'?>
<project>
  <description>No-op job for testing jenkins functionality</description>
  <keepDependencies>false</keepDependencies>
  <properties/>
  <scm class="hudson.scm.NullSCM"/>
  <canRoam>true</canRoam>
  <disabled>false</disabled>
  <blockBuildWhenDownstreamBuilding>false</blockBuildWhenDownstreamBuilding>
  <blockBuildWhenUpstreamBuilding>false</blockBuildWhenUpstreamBuilding>
  <triggers/>
  <concurrentBuild>false</concurrentBuild>
  <builders/>
  <publishers/>
  <buildWrappers/>
</project>
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

    def create_subscribed_content_library(self, name, url, datastore):
        spec = {
            "client_token": str(uuid.uuid4()),
            "create_spec": {
                "name": name,
                "description": "",
                "type": "SUBSCRIBED",
                "subscription_info": {
                    "subscription_url": url,
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
    def __init__(self, server, username, password, directory):
        self.kubeconfig = os.path.join(directory, "kubeconfig")
        os.environ["KUBECONFIG"] = self.kubeconfig

        self.bin_dir = os.path.join(directory, "bin")
        sys.path.insert(0, self.bin_dir)
        os.environ["PATH"] += os.pathsep + self.bin_dir
        self.kubectl = os.path.join(self.bin_dir, "kubectl")

        self.vsphere_plugin_download(server, directory)
        self.vsphere_login(server, username, password)

    def vsphere_plugin_download(self, server, directory):
        # Download kubectl vsphere plugin and extract
        # TODO: Maybe this can be baked in to the docker image?

        if os.uname().sysname == "Darwin":
            plugin_url = (
                "https://%s/wcp/plugin/darwin-amd64/vsphere-plugin.zip" % server
            )
        else:
            plugin_url = "https://%s/wcp/plugin/linux-amd64/vsphere-plugin.zip" % server
        plugin_zipfile = os.path.join(temp_dir, "vsphere-plugin.zip")
        with requests.get(plugin_url, verify=False, stream=True) as res:
            res.raise_for_status()
            with open(plugin_zipfile, "wb") as f:
                shutil.copyfileobj(res.raw, f)
        with zipfile.ZipFile(plugin_zipfile) as zip:
            zip.extractall(directory)  # Unzips to bin directory

        os.chmod(os.path.join(self.bin_dir, "kubectl"), 0o777)
        os.chmod(os.path.join(self.bin_dir, "kubectl-vsphere"), 0o777)

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
        ) as child:
            child.expect("Password:")
            child.sendline(password)
            child.wait()
        if child.exitstatus:
            raise Exception("Failed to login via kubectl vsphere plugin")


class Bitnami(object):
    def __init__(self, ip, sshkey):
        self.ssh = paramiko.client.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.client.WarningPolicy)
        self.connect()
        self.username, self.password = self.extract_creds()

    def connect(self):
        for _ in retry(delay=10, timeout=300):
            try:
                log.debug("Attempting to SSH in to %s", vm_ip)
                self.ssh.connect(
                    hostname=vm_ip,
                    username="bitnami",
                    pkey=sshkey,
                    look_for_keys=False,
                    timeout=10,
                )
                break
            except socket.timeout:
                continue

    def extract_creds(self):
        sftp = self.ssh.open_sftp()
        for _ in retry(delay=10, timeout=300):
            try:
                log.debug("Attempting to read bitnami_credentials")
                with sftp.open("/home/bitnami/bitnami_credentials") as fd:
                    creds_re = re.search(
                        b"username and password is '(?P<username>.*)' and '(?P<password>.*)'",
                        fd.read(),
                    )
                    if creds_re is None:
                        continue
                    jenkins_username = creds_re.group("username")
                    jenkins_password = creds_re.group("password")
                    if jenkins_username and jenkins_password:
                        break
            except FileNotFoundError:
                continue

        log.info(
            "Jenkins credentials: username='%s' password='%s'",
            jenkins_username,
            jenkins_password,
        )
        return (jenkins_username, jenkins_password)


class Jenkins(object):
    def __init__(self, ip, username, password):
        self.ip = ip
        self.session = requests.sessions.Session()
        self.session.auth = (username, password)
        self.session.verify = False
        # self.session.headers.update({"Content-Type":"application/json"})

        adapter = requests.adapters.HTTPAdapter(pool_connections=1)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.authenticate()

    def url(self, path):
        return "https://%s/%s" % (self.ip, path.lstrip("/"))

    def authenticate(self):
        for _ in retry(10, 300):
            try:
                res = self.session.get(
                    self.url("/api/json"), headers={"Content-Type": "text/json"}
                )
                res.raise_for_status()
            except Exception:
                continue
            if res.json().get("mode") == "NORMAL":
                break

        res = self.session.get(
            self.url(
                '/crumbIssuer/api/xml?xpath=concat(//crumbRequestField,":",//crumb)'
            )
        )
        res.raise_for_status()
        key, crumb = res.text.split(":")
        self.session.headers[key] = crumb

    def create_job(self, jobname):
        res = self.session.post(
            self.url("/createItem?name=%s" % jobname),
            headers={"Content-Type": "text/xml"},
            data=JENKINS_JOB_CONFIG,
        )
        res.raise_for_status()

    def build_job(self, jobname):
        res = self.session.post(self.url("/job/%s/build" % jobname))
        res.raise_for_status()

        queue_url = res.headers["Location"]

        for _ in retry(5, 60):
            try:
                res = self.session.get(
                    "%sapi/json" % queue_url, headers={"Content-Type": "text/json"}
                )
            except requests.exceptions.ConnectionError:
                continue
            res.raise_for_status()
            build_url = res.json().get("executable", {}).get("url")
            if build_url is not None and build_url != "null":
                break

        for _ in retry(5, 60):
            try:
                res = self.session.get(
                    "%sapi/json" % build_url, headers={"Content-Type": "text/json"}
                )
            except requests.exceptions.ConnectionError:
                continue
            res.raise_for_status()
            if res.json().get("result") == "SUCCESS":
                break

        return build_url


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
        description="Configure and deploy a test workload (Jenkins) to a WCP Testbed"
    )
    parser.add_argument("--vc", help="vCenter IP or Hostname", required=True)
    parser.add_argument(
        "--vc-username", help="vSphere username", default="Administrator@vsphere.local"
    )
    parser.add_argument("--vc-password", help="vSphere password", default="Admin!23")
    parser.add_argument(
        "--namespace", help="WCP Namespace to deploy workload", required=True
    )
    parser.add_argument(
        "--workload-name",
        help="Name to give the deployed workload",
        default="jenkins-workload-%d" % int(time.time()),
    )
    parser.add_argument(
        "--cl-url",
        help="Content library subscription url with the workload image",
        default="http://sc-dbc2110.eng.vmware.com/jkarunaratne/tmp/bitnami-jenkins-2.222.3-1--cl/lib.json",
    )
    parser.add_argument(
        "--image-name",
        help="Image name within the content library",
        default="bitnami-jenkins-2.222.3-1",
    )
    parser.add_argument(
        "--storage-policy",
        help="Storage policy to use for deployment",
        default="wcpglobal_storage_profile",
    )
    args = parser.parse_args()

    # Temporary working directory
    temp_dir = tempfile.mkdtemp()
    log.info("Using temporary directory %s", temp_dir)

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
            "bitnami-jenkins-cl", args.cl_url, nfs_datastores[0]["datastore"]
        )

    # Configure cluster
    vc.ensure_cluster_has_content_library(namespace["cluster"], cl["id"])

    # Get k8s endpoint
    cluster = vc.get_cluster(namespace["cluster"])
    k8s_ip = cluster.get("api_server_management_endpoint")

    # Prepare and login via kubectl vsphere
    kubectl_vsphere = KubectlVSphere(
        k8s_ip, args.vc_username, args.vc_password, temp_dir
    )

    # Connect to k8s
    kubernetes.config.load_kube_config(config_file=kubectl_vsphere.kubeconfig)
    k_v1 = kubernetes.client.CoreV1Api()
    k_ext = kubernetes.client.ExtensionsV1beta1Api()
    k_cust = kubernetes.client.CustomObjectsApi()

    # Generate ssh key for vm
    sshkey = paramiko.RSAKey.generate(1024)
    sshkey_filepath = os.path.join(temp_dir, "sshkey")
    sshkey.write_private_key_file(sshkey_filepath)

    # Prepare manifest
    params = {
        "deployment_namespace": args.namespace,
        "deployment_name": args.workload_name,
        "deployment_image": args.image_name,
        "ssh_pubkey": "%s %s" % (sshkey.get_name(), sshkey.get_base64()),
    }
    vm_manifest = yaml.safe_load(string.Template(VM_MANIFEST).substitute(params))
    vm_metadata_configmap = yaml.safe_load(
        string.Template(VM_METADATA_CONFIGMAP).substitute(params)
    )

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

    # Get the bitnami creds via SSH
    bitnami = Bitnami(vm_ip, sshkey)

    # Log in to Jenkins and create a job
    jenkins = Jenkins(vm_ip, bitnami.username, bitnami.password)
    jenkins.create_job(args.workload_name)
    build_url = jenkins.build_job(args.workload_name)

    # Success!
    log.info("Successfully deployed jenkins, created a job, and built it")
    log.info(
        "Username '%s', Password '%s', Build '%s'",
        bitnami.username,
        bitnami.password,
        build_url,
    )
