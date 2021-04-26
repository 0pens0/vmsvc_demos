export KUBECTL_VSPHERE_PASSWORD=VMware1!
echo "Login to the supervisor cluster"
kubectl vsphere login --server https://192.10.136.1/ -u 'administrator@vsphere.local' --insecure-skip-tls-verify
kubectl config use-context 192.10.136.1
echo "Create a new Test Namespace"
kubectl create ns automation
echo "Add test-class machineclass and vm-svc content library to the Namespace configuration"
curl --location --request PATCH 'https://openso-vcsa.lab.local/api/vcenter/namespaces/instances/automation' \
--header 'vmware-api-session-id: f9fc3f7089716da8a290ba886fe4cee1' \
--header 'Content-Type: application/json' \
--header 'Cookie: vmware-api-session-id=f9fc3f7089716da8a290ba886fe4cee1' \
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
            "best-effort-xsmall"
        ],
        "content_libraries": [
            "8fab3681-4498-40ef-9bd8-92d92663868e"
        ]
    },
    "self_service_namespace": false,
    "config_status": "RUNNING",
    "storage_specs": [
        {
            "policy": "80177dfa-5425-4ae0-a537-fae8e3add6ea"
        }
    ]
}' -k
echo "Deploy two Ubuntu machines with docker installed and Nginx container exposed in port 80 with Loadbalancer service"
kubectl create -f vm-ubuntu-nginx.yaml -n automation

