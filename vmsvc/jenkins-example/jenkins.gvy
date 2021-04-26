#!groovy
// Copyright (c) 2019 VMware, Inc. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

def workloadPath = "sample/jenkins-example"
def dockerImage = "vmop-jenkins-workload-tester:${BUILD_NUMBER}" // tester image name

pipeline {
    agent {
        node {
            label 'nimbus-cloud'
            customWorkspace "workspace/$JOB_NAME/$BUILD_NUMBER"
        }
    }

    parameters {
        string(description: '[Required] The git branch name or commit sha1 in vm-operator.git repository to build', name: 'BRANCH', defaultValue: 'origin/master')
        string(description: '[Required] A reachable IP address of the VCSA', name: 'VC_IP')
        string(description: '[Required] vSphere username with edit permissions for the WCP_NAMESPACE', name: 'VC_USERNAME', defaultValue: 'Administrator@vsphere.local')
        password(description: '[Required] vSphere user password', name: 'VC_PASSWORD', defaultValue: 'Admin!23')
        string(description: '[Required] Existing WCP Namespace with edit permissions for specified user VC_USERNAME', name: 'WCP_NAMESPACE', defaultValue: 'my-podvm-ns')
        string(description: '[Required] vSphere storage policy', name: 'VC_STORAGE_POLICY', defaultValue: 'wcpglobal_storage_profile')
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
                        sh "docker build --no-cache --rm=true -t ${dockerImage} -f Dockerfile ."
                    }
                }
            }
        }

        stage('deploy and test') {
            steps {
                script {
                    runArgs = "--vc ${params.VC_IP} --vc-username ${params.VC_USERNAME} --vc-password ${params.VC_PASSWORD} --namespace ${params.WCP_NAMESPACE} --storage-policy ${params.VC_STORAGE_POLICY}"
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
