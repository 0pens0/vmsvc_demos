# Example Jenkins Deployment

This is an example of how to deploy Jenkins as an example workload via vm-operator.

## Steps

### vSphere Testbed

- Deploy a vSphere testbed
- Create a wcp cluster
- Create a namespace
- Create a storage profile with some datastores

The recomended way to do the above is to use the https://wcp.svc.eng.vmware.com/job/dev-selfservice-testbed/ with the feature state switch: `WCP_VMService=enabled`. This job will automatically create a namespace `my-podvm-ns` and a storage policy `wcpglobal_storage_profile`.

### Deploying the workload

The test workload can be deployed on the vSphere testbed by running the `deploy_jenkins.py` which can be built with in a docker container.

```sh
IMAGETAG=jenkins-example
docker build -t $IMAGETAG .
docker run -t --rm $IMAGETAG --vc "1.2.3.4" --namespace "my-podvm-ns"
```

The script within the docker image will automatically do the following:

- Configure a content-library subscription to `http://sc-dbc2110.eng.vmware.com/jkarunaratne/tmp/bitnami-jenkins-2.222.3-1--cl/lib.json`
- Assign the content-library to the cluster that has the provided namespace
- Assign the vsphere user's edit permissions to the cluster
- Assign the storage policiy specified to the cluster
- Create a virtual machine deployment manifest with a metadata configmap

## Refs on OvfEnv

- https://marketplace.vmware.com/vsx/solutions/jenkins-ova-2-222-3-1?ref=try-button
- https://bitnami.com/stack/jenkins/virtual-machine
- https://coreos.com/os/docs/latest/booting-on-vmware.html
