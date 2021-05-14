#!/bin/sh

# Automation script to patch a NS in vCenter

# Get the namespace
if [ $# -eq 0 ]
then
        echo "Please provide namespace: "
        read NAMESPACE
else
        NAMESPACE="$1"
fi

# Check if we got one
if [ -z "$NAMESPACE" ]; then
        echo "Namespace not provided!"
        exit 1
fi

# Set the vCenter
VCENTER="openso-vcsa.lab.local"

# Fetch vCSA API token if not set
if [ -z "$VCENTER_API_SESSION_ID" ]; then
        # Fetch vCSA API token
        echo '## Getting vCSA API token ##'
        VCENTER_API_SESSION_ID=$( curl --location --insecure --silent --request POST "https://${VCENTER}/rest/com/vmware/cis/session" \
        --header 'Content-Type: application/json' \
        --header 'Authorization: Basic YWRtaW5pc3RyYXRvckB2c3BoZXJlLmxvY2FsOlZNd2FyZTEh' \
        | jq -r '.value' )
fi

echo "vCSA session ID: ${VCENTER_API_SESSION_ID}"

# Patch the namespace through the API
echo "## Add test-class machineclass and vm-svc content library to the Namespace configuration ##"
curl --location --request PATCH "https://${VCENTER}/api/vcenter/namespaces/instances/${NAMESPACE}" \
--header 'vmware-api-session-id: '${VCENTER_API_SESSION_ID} \
--header 'Content-Type: application/json' \
--header 'Cookie: vmware-api-session-id='${VCENTER_API_SESSION_ID} \
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

