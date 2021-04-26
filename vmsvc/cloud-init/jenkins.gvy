#!groovy
// Copyright (c) 2020 VMware, Inc. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

def workloadPath = "sample/cloud-init"
def dockerImage = "vmop-cloud-init-tester:${BUILD_NUMBER}" // tester image name
def cloudConfigFile = "cloud-config.yaml"
def kubeConfigFile = "kubeconfig"

def default_cloud_init_config = '''#cloud-config

users:
  - default
  - name: vmware
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    # Password set to Admin!23
    passwd: '$1$salt$SOC33fVbA/ZxeIwD5yw1u1'
    shell: /bin/bash
    ssh_authorized_keys:
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIeitJZbrt4l1xaRGgNAeRXZGEGlZDIuAf8rqze4Iukc

ssh_pwauth: true

write_files:
  - path: /helloworld
    content: |
      VMSVC Says Hello World

apt:
  preserve_sources_list: true
  sources:
    elasticsearch:
      source: deb https://artifacts.elastic.co/packages/7.x/apt stable main
      keyid: D88E42B4

package_update: true
packages:
  - apt-transport-https
  - elasticsearch
  - kibana

runcmd:
  - "echo 'server.host: 0.0.0.0' >> /etc/kibana/kibana.yml"
  - systemctl daemon-reload
  - systemctl enable elasticsearch.service
  - systemctl restart elasticsearch.service
  - systemctl enable kibana.service
  - systemctl restart kibana.service
'''

pipeline {
    agent {
        node {
            label 'nimbus-cloud'
            customWorkspace "workspace/$JOB_NAME/$BUILD_NUMBER"
        }
    }

    parameters {
        string(description: 'The git branch name or commit sha1 in vm-operator.git repository to build', name: 'BRANCH', defaultValue: 'origin/master')
        string(description: 'vSphere: IP of VCSA', name: 'VC_IP')
        string(description: 'vSphere: Username with edit access to WCP_NAMESPACE', name: 'VC_USERNAME', defaultValue: 'Administrator@vsphere.local')
        password(description: 'vSphere: Password', name: 'VC_PASSWORD', defaultValue: 'Admin!23')
        string(description: 'vSphere storage policy', name: 'VC_STORAGE_POLICY', defaultValue: 'wcpglobal_storage_profile')
        string(description: 'Content Library URL', name: 'CL_URL', defaultValue: 'http://sc-dbc2110.eng.vmware.com/jkarunaratne/tmp/ubuntu-groovy-20.10-cloudimg--cl/lib.json')
        string(description: 'Content Library Name', name: 'CL_NAME', defaultValue: 'vmsvc-workload-cl')
        string(description: 'Workload: Image Name (must be available on the provided CL)', name: 'IMAGE_NAME', defaultValue: 'ubuntu-groovy-20.10-cloudimg')
        string(description: 'Workload: Namespace', name: 'WCP_NAMESPACE', defaultValue: 'my-podvm-ns')
        text(description: 'Workload: Cloud-init configuration', name: 'CLOUD_INIT_CONFIG', defaultValue: "${default_cloud_init_config}")
    }

    options {
        skipDefaultCheckout true
        gitLabConnection 'Gitlab-eng'
        timestamps() // Requires timestamp plugin
        buildDiscarder(logRotator(numToKeepStr: '30'))
        timeout(time: 30, unit: 'MINUTES')
    }

    stages {
        stage('init') {
            steps {
                script {
                    checkout scm
                    dir(workloadPath) {
                        writeFile(file: "${cloudConfigFile}", text: "${default_cloud_init_config}")
                        sh "docker build --no-cache --rm=true -t ${dockerImage} -f Dockerfile ."
                    }
                }
            }
        }

        stage('deploy and test') {
            steps {
                script {
                    workloadName = "vmsvc-cloudinit-${JOB_NAME}-${BUILD_NUMBER}"
                    runArgs = "--vc ${params.VC_IP} --vc-username ${params.VC_USERNAME} --vc-password ${params.VC_PASSWORD} --namespace ${params.WCP_NAMESPACE} --storage-policy ${params.VC_STORAGE_POLICY} --cl-url ${params.CL_URL} --cl-name ${params.CL_NAME} --image-name ${params.IMAGE_NAME} --workload-name ${workloadName}"
                    sh "docker run -t --rm ${dockerImage} ${runArgs}"
                }
            }
        }
    }

    post {
        always {
            script {
                sh "docker rmi -f ${dockerImage}"
            }
            cleanWs()
        }
    }
}
