# !/bin/sh
# Automation script to create a NS in vCneter and deploy VM's using the VM operator

# Colors
RED="\e[01;31m"
GREEN="\e[01;32m"
YELLOW="\e[01;33m"
BLUE="\e[01;34m"
COLOROFF="\e[00m"

export SUP="10.193.132.129"
export VCENTER="sc3-vc-01.haas-232.pez.pivotal.io"
USER_NAME="$1"
NAMESPACE="$2"


echo "## Login to the supervisor cluster ##"


kubectl vsphere login --server http://$SUP/ -u $USER_NAME --insecure-skip-tls-verify


echo "## Create a new Test Namespace ##"
kubectl create ns $NAMESPACE


echo "## Add test-class machineclass and vm-svc content library to the Namespace configuration ##"
curl --location --request PATCH 'https://${VCENTER}/api/vcenter/namespaces/instances/${NAMESPACE}' \
--header 'vmware-api-session-id: 67fa08f8007edf1c097f693a2433e916' \
--header 'Content-Type: application/json' \
--header 'Cookie: vmware-api-session-id=67fa08f8007edf1c097f693a2433e916' \
--data-raw '{
    "cluster": "domain-c8",
    "stats": {
        "cpu_used": 0,
        "memory_used": 0,
        "storage_used": 0
    },
    "description": "",
    "messages": [],
    "access_list": [
        {
            "role": "OWNER",
            "subject_type": "USER",
            "subject": "Administrator",
            "domain": "vsphere.local"
        }
    ],
    "vm_service_spec": {
        "vm_classes": [
            "test-class"
        ],
        "content_libraries": [
            "9a0c6212-c8ce-4b7f-96b3-8fc4b9a52871"
        ]
    },
    "self_service_namespace": false,
    "config_status": "RUNNING",
    "storage_specs": [
        {
            "policy": "f143f3de-40de-4084-b287-08ef86497d14"
        }
    ]
}' -k


echo "## Deploy two Ubuntu machines with docker installed and Nginx container exposed in port 80 with Loadbalancer service ##"
kubectl create -f vm-ubuntu-nginx.yaml -n test
