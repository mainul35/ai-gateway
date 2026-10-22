// Builds one commit of ai-gateway and pushes it to Harbor. Deploying it is a separate job
// (deploy-ai-gateway) -- this one only ever produces tagged, pushed images; nothing here touches
// homelabai or the running wake front.
//
// Follows vsd-auth-ops/jenkins/build-authserver.Jenkinsfile. Two images, one tag (the short commit):
//   gateway-release     the gateway's code for that commit (jenkins/release.Dockerfile). The gateway
//                       itself does not run in Docker -- it needs the GPU machine's llama.cpp,
//                       ComfyUI, models and project folders -- so the deploy job copies the code out
//                       of this image and deploys it with bin/deploy.sh, as before.
//   gateway-wake-front  the wake front (wake/), which does run as a container on proxy-vm.
//
// Run it with "Build with Parameters" and a branch: nothing triggers it automatically.
// Setup: docs/setup-guide.md, Parts 4 to 8.
pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    // NOTE: `BRANCH` is defined in the JOB UI as an Active Choices parameter -- a live dropdown of
    // ai-gateway's branches (docs/setup-guide.md, step 7.2) -- NOT here. A declarative `parameters { }` block would
    // overwrite that UI-defined parameter on every run, so it is intentionally omitted.

    environment {
        // The host's own Docker daemon (socket mounted into the Jenkins container), where Harbor's
        // nginx listens on :5000.
        REGISTRY       = 'localhost:5000'
        HARBOR_PROJECT = 'ai-gateway'
        APP_REPO       = 'https://github.com/mainul35/ai-gateway.git'
    }

    stages {
        // Fails in a second if the robot secret is wrong or expired, instead of after the checks
        // and builds. Logs straight back out: the push stage does its own login.
        stage('Check registry credentials') {
            steps {
                timeout(time: 2, unit: 'MINUTES') {
                    withCredentials([usernamePassword(
                        credentialsId: 'harbor-robot-ai-gateway',
                        usernameVariable: 'HARBOR_USER',
                        passwordVariable: 'HARBOR_PASS'
                    )]) {
                        sh '''
                            if ! echo "$HARBOR_PASS" | docker login ${REGISTRY} -u "$HARBOR_USER" --password-stdin; then
                                echo ""
                                echo "Harbor rejected the robot credential before anything was built."
                                echo "Check Projects > ai-gateway > Robot Accounts > jenkins-ai-gateway:"
                                echo "  - the Expires column (project robots default to 30 days)"
                                echo "  - whether Refresh Secret was used, which revokes the old value"
                                exit 1
                            fi
                            docker logout ${REGISTRY}
                        '''
                    }
                }
            }
        }

        stage('Checkout') {
            steps {
                timeout(time: 10, unit: 'MINUTES') {
                    // Public repository: no credential. The Jenkinsfile comes from the job's own SCM
                    // checkout; this pulls the app at the chosen BRANCH, which may be another branch.
                    git url: env.APP_REPO, branch: params.BRANCH ?: 'develop'
                    script {
                        env.IMAGE_TAG     = sh(script: 'git rev-parse --short=7 HEAD', returnStdout: true).trim()
                        env.RELEASE_IMAGE = "${REGISTRY}/${HARBOR_PROJECT}/gateway-release"
                        env.WAKE_IMAGE    = "${REGISTRY}/${HARBOR_PROJECT}/gateway-wake-front"
                        echo "Building ${env.IMAGE_TAG} from branch '${params.BRANCH ?: 'develop'}'"
                    }
                }
            }
        }

        // ci/checks.sh: compile, control characters, secret scan, shell syntax, wake front tests.
        // The Jenkins image has no Python, so it runs in python:3.12 through the host's Docker,
        // reaching the workspace with --volumes-from this container (as vsd-auth-server-enterprise's
        // Maven stage does). Same uid as Jenkins, so it can write .ci-venv into the workspace.
        stage('Checks') {
            steps {
                timeout(time: 15, unit: 'MINUTES') {
                    sh '''
                        docker run --rm \
                            --volumes-from "$(hostname)" \
                            -w "$WORKSPACE" \
                            -u "$(id -u):$(id -g)" \
                            -e HOME="$WORKSPACE/.ci-home" \
                            python:3.12 \
                            bash -c 'mkdir -p "$HOME" && git config --global --add safe.directory "$PWD" && ci/checks.sh'
                    '''
                }
            }
        }

        stage('Build images') {
            steps {
                timeout(time: 15, unit: 'MINUTES') {
                    sh '''
                        set -e
                        # The release image holds exactly what git holds for this commit: nothing
                        # left over in the workspace (.ci-venv, .ci-home) and no .dockerignore rules
                        rm -rf .release-context && mkdir .release-context
                        git archive HEAD | tar -x -C .release-context
                        rm -f .release-context/.dockerignore
                        cp jenkins/release.Dockerfile .release-context/Dockerfile.release
                        docker build -q -f .release-context/Dockerfile.release \
                            --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
                            --label "org.opencontainers.image.ref.name=${BRANCH:-develop}" \
                            -t "$RELEASE_IMAGE:$IMAGE_TAG" -t "$RELEASE_IMAGE:latest" .release-context
                        rm -rf .release-context

                        docker build -q -t "$WAKE_IMAGE:$IMAGE_TAG" -t "$WAKE_IMAGE:latest" wake
                    '''
                }
            }
        }

        stage('Push to Harbor') {
            steps {
                timeout(time: 10, unit: 'MINUTES') {
                    withCredentials([usernamePassword(
                        credentialsId: 'harbor-robot-ai-gateway',
                        usernameVariable: 'HARBOR_USER',
                        passwordVariable: 'HARBOR_PASS'
                    )]) {
                        sh '''
                            set -e
                            echo "$HARBOR_PASS" | docker login ${REGISTRY} -u "$HARBOR_USER" --password-stdin
                            for image in "$RELEASE_IMAGE" "$WAKE_IMAGE"; do
                                docker push "$image:$IMAGE_TAG"
                                docker push "$image:latest"
                            done
                            docker logout ${REGISTRY}
                        '''
                    }
                }
            }
        }
    }

    post {
        success {
            echo "Pushed ${IMAGE_TAG} (gateway-release + gateway-wake-front) -- pick tag '${IMAGE_TAG}' in deploy-ai-gateway to release it."
        }
        always {
            // Only dangling layers older than three days -- never what this build just pushed.
            sh 'docker image prune -f --filter "until=72h" || true'
        }
    }
}
