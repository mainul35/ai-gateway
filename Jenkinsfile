// AI gateway pipeline, for jenkins.mainul35.dev on proxy-vm.
//
// Every branch and pull request: the checks (ci/checks.sh).
// develop, once the checks pass:
//   1. wake homelabai if it is asleep, through the wake front      (bin/wake-homelabai.sh)
//   2. deploy the gateway to homelabai over ssh                      (bin/deploy.sh: compile check,
//      backup, sync, changed requirements, restart, health check, automatic rollback, public check)
//   3. deploy the wake front on proxy-vm                             (bin/deploy-wake.sh: rebuild,
//      restart only if it changed, health check, automatic rollback)
//
// Jenkins setup (credentials, job, webhook): docs/deployment.md.

def isDeployBranch() {
    // BRANCH_NAME in a multibranch job; GIT_BRANCH ("origin/develop") in a plain pipeline job
    return env.BRANCH_NAME == 'develop' || (env.GIT_BRANCH ?: '').endsWith('/develop')
}

pipeline {
    agent any

    options {
        disableConcurrentBuilds()                 // two deploys at once would trip over each other
        timeout(time: 30, unit: 'MINUTES')        // waking can take 5 min, a requirements install longer
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    environment {
        GATEWAY_SSH    = 'mainul35@192.168.50.146'
        GATEWAY_URL    = 'http://192.168.50.146:9000'
        WAKE_FRONT_URL = 'http://192.168.50.81:9100'
        PUBLIC_URL     = 'https://ai-gateway.mainul35.dev'
    }

    stages {
        stage('Checks') {
            steps {
                sh 'ci/checks.sh'
            }
        }

        stage('Wake homelabai') {
            when { expression { isDeployBranch() } }
            steps {
                sh 'bin/wake-homelabai.sh'
            }
        }

        stage('Deploy gateway') {
            when { expression { isDeployBranch() } }
            steps {
                withCredentials([sshUserPrivateKey(credentialsId: 'homelabai-ssh', keyFileVariable: 'SSH_KEY')]) {
                    sh '''
                        DEPLOY_HOST="$GATEWAY_SSH" \
                        DEPLOY_PUBLIC_URL="$PUBLIC_URL" \
                        DEPLOY_SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
                        bin/deploy.sh
                    '''
                }
            }
        }

        stage('Deploy wake front') {
            when { expression { isDeployBranch() } }
            steps {
                withCredentials([file(credentialsId: 'gateway-wake-env', variable: 'WAKE_ENV_FILE')]) {
                    sh 'WAKE_DIR="$WORKSPACE/.wake-front" bin/deploy-wake.sh'
                }
            }
        }
    }

    post {
        success {
            script {
                if (isDeployBranch()) {
                    def commit = env.GIT_COMMIT ? env.GIT_COMMIT.substring(0, 7) : 'this build'
                    echo "Deployed ${commit}: gateway on homelabai, wake front on proxy-vm"
                }
            }
        }
        failure {
            echo 'A failed deploy step has already rolled itself back; its log above says which step and why.'
        }
    }
}
