// Deploys one already-built tag of ai-gateway from Harbor. Building is a separate job
// (build-ai-gateway) -- this one never builds anything, only pulls and deploys what is already in
// the registry. Rollback: run it again and pick the previous tag.
//
// Follows vsd-auth-ops/jenkins/deploy-authserver.Jenkinsfile. A tag is one commit, and deploys both:
//   the gateway     code copied out of gateway-release:<tag>, deployed to homelabai over ssh with
//                   that commit's own bin/deploy.sh (backup, sync, requirements, restart, health
//                   check, automatic rollback). homelabai is woken first if it is asleep.
//   the wake front  gateway-wake-front:<tag>, run with docker compose on proxy-vm.
//
// Run it with "Build Now"; it pauses to ask which tag. Nothing triggers it automatically.
// Setup: jenkins/SETUP.md.
pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    environment {
        REGISTRY       = 'localhost:5000'
        HARBOR_PROJECT = 'ai-gateway'
        RELEASE_REPO   = 'gateway-release'
        WAKE_REPO      = 'gateway-wake-front'

        GATEWAY_SSH    = 'mainul35@192.168.50.146'
        GATEWAY_URL    = 'http://192.168.50.146:9000'
        WAKE_FRONT_URL = 'http://192.168.50.81:9100'
        PUBLIC_URL     = 'https://ai-gateway.mainul35.dev'

        // The wake front's compose project. bin/deploy-wake.sh uses the same name, so a deploy by
        // hand and a deploy from here manage the same container.
        COMPOSE_PROJECT_NAME = 'gateway-wake'

        // Tags offered in the picker, newest first, and how many are kept in Harbor. A homelab
        // gateway is deployed now and then, not daily, so this counts tags rather than hours.
        OFFER_TAGS = '15'
        KEEP_TAGS  = '20'
    }

    stages {
        stage('Select image tag') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'harbor-robot-ai-gateway',
                    usernameVariable: 'HARBOR_USER',
                    passwordVariable: 'HARBOR_PASS'
                )]) {
                    script {
                        // Same approach as deploy-authserver: the query runs in a throwaway alpine
                        // container on the host network, and travels in as an env var rather than a
                        // bind-mounted file (a workspace path means nothing to the host daemon).
                        def harborScript = '''set -e
HTTP_STATUS=$(curl -s -o /tmp/artifacts.json -w "%{http_code}" -u "$HARBOR_USER:$HARBOR_PASS" "http://${REGISTRY}/api/v2.0/projects/${HARBOR_PROJECT}/repositories/${RELEASE_REPO}/artifacts?with_tag=true&page_size=100")
if [ "$HTTP_STATUS" != "200" ]; then
  echo "Harbor answered $HTTP_STATUS: $(cat /tmp/artifacts.json)" >&2
  exit 1
fi
jq -r '[ .[] | (.tags // []) as $tags | ($tags | any(.name == "latest")) as $hasLatest | (.extra_attrs.config.Labels["org.opencontainers.image.ref.name"] // "") as $branch | ($tags[] | select(.name != "latest")) as $tag | {name: $tag.name, push_time: $tag.push_time, latest: $hasLatest, branch: $branch} ] | sort_by(.push_time) | reverse | .[] | (.name) + "|" + (.push_time) + "|" + (if .latest then "latest" else "" end) + "|" + .branch' /tmp/artifacts.json
'''
                        def artifactsRaw = withEnv(["HARBOR_SCRIPT=${harborScript}"]) {
                            retry(3) {
                                timeout(time: 2, unit: 'MINUTES') {
                                    sh(
                                        script: '''docker run --rm --network host -e HARBOR_USER -e HARBOR_PASS -e REGISTRY -e HARBOR_PROJECT -e RELEASE_REPO -e HARBOR_SCRIPT alpine:3.20 sh -c 'apk add --no-cache curl jq >/dev/null && echo "$HARBOR_SCRIPT" | sh' ''',
                                        returnStdout: true
                                    )
                                }
                            }
                        }.trim()

                        def offer = env.OFFER_TAGS.toInteger()
                        def keep = env.KEEP_TAGS.toInteger()
                        def labels = []
                        def stale = []
                        // A plain loop: closures with an index are unreliable under Jenkins' CPS
                        def lines = artifactsRaw.readLines()
                        for (int i = 0; i < lines.size(); i++) {
                            def parts = lines[i].split('\\|', -1)
                            if (parts.length < 2) {
                                continue
                            }
                            def tagName = parts[0]
                            if (i < offer) {
                                def when = parts[1].replace('T', ' ')
                                when = when.length() > 19 ? when.substring(0, 19) : when
                                def label = "${tagName}  (${when} UTC"
                                if (parts.length > 3 && parts[3]) {
                                    label += ", ${parts[3]}"
                                }
                                label += ')'
                                if (parts.length > 2 && parts[2] == 'latest') {
                                    label += ' [latest]'
                                }
                                labels << label
                            }
                            if (i >= keep) {
                                stale << tagName
                            }
                        }
                        env.STALE_TAGS = stale.join(',')
                        if (labels.isEmpty()) {
                            error("No ${RELEASE_REPO} tags in Harbor yet -- run build-ai-gateway first.")
                        }

                        // Waits for someone to pick; capped so an abandoned deploy frees the executor.
                        def selected = timeout(time: 60, unit: 'MINUTES') {
                            input(
                                message: 'Select the ai-gateway tag to deploy',
                                parameters: [choice(
                                    name: 'IMAGE_TAG',
                                    choices: labels.join('\n'),
                                    description: 'Newest first. Each tag deploys that commit of the gateway (homelabai) and the wake front (proxy-vm).'
                                )]
                            )
                        }
                        env.SELECTED_TAG = selected.split(' ')[0]
                        echo "Deploying ${env.SELECTED_TAG}"
                    }
                }
            }
        }

        stage('Pull release') {
            steps {
                timeout(time: 10, unit: 'MINUTES') {
                    withCredentials([usernamePassword(
                        credentialsId: 'harbor-robot-ai-gateway',
                        usernameVariable: 'HARBOR_USER',
                        passwordVariable: 'HARBOR_PASS'
                    )]) {
                        sh '''
                            set -e
                            RELEASE="${REGISTRY}/${HARBOR_PROJECT}/${RELEASE_REPO}:${SELECTED_TAG}"
                            WAKE="${REGISTRY}/${HARBOR_PROJECT}/${WAKE_REPO}:${SELECTED_TAG}"
                            echo "$HARBOR_PASS" | docker login ${REGISTRY} -u "$HARBOR_USER" --password-stdin
                            docker pull -q "$RELEASE"
                            docker pull -q "$WAKE"
                            docker logout ${REGISTRY}

                            # The gateway's code for this tag, copied out through the Docker API,
                            # so it lands in this workspace whatever the host's paths are.
                            rm -rf release
                            cid=$(docker create "$RELEASE" /none)
                            docker cp "$cid:/src" release
                            docker rm "$cid" >/dev/null
                            echo "release $SELECTED_TAG: $(ls release | tr '\\n' ' ')"
                        '''
                    }
                }
            }
        }

        stage('Wake homelabai') {
            steps {
                timeout(time: 10, unit: 'MINUTES') {
                    // Through the wake front, from a container on proxy-vm's own network. This
                    // Jenkins container's broadcasts never reach the LAN, and its requests to
                    // 192.168.50.81:9100 - this very machine, from inside Docker - are dropped by the
                    // host's firewall, so the wake never went out: a deploy started while homelabai
                    // slept stalled here. On the host network the wake front is plain 127.0.0.1, as
                    // Harbor is for the stages above. The script goes in as a variable, not a mount:
                    // Docker here is the host's, which would look for this workspace on its own disk.
                    sh '''
                        WAKE_SCRIPT="$(cat release/bin/wake-homelabai.sh)" docker run --rm --network host \\
                            -e WAKE_SCRIPT -e GATEWAY_URL -e WAKE_FRONT_URL=http://127.0.0.1:9100 -e WAKE_ENV=/nonexistent \\
                            alpine:3.20 sh -c 'apk add --no-cache bash curl >/dev/null && printf "%s\\n" "$WAKE_SCRIPT" | bash'
                    '''
                }
            }
        }

        stage('Deploy gateway') {
            steps {
                timeout(time: 20, unit: 'MINUTES') {
                    withCredentials([sshUserPrivateKey(credentialsId: 'homelabai-ssh', keyFileVariable: 'SSH_KEY')]) {
                        // DEPLOY_SKIP_CHECKS: build-ai-gateway already ran ci/checks.sh on this exact
                        // commit, and this agent has no Python to run them again
                        sh '''
                            DEPLOY_HOST="$GATEWAY_SSH" \
                            DEPLOY_PUBLIC_URL="$PUBLIC_URL" \
                            DEPLOY_SKIP_CHECKS=1 \
                            DEPLOY_SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
                            release/bin/deploy.sh
                        '''
                    }
                }
            }
        }

        stage('Deploy wake front') {
            steps {
                timeout(time: 5, unit: 'MINUTES') {
                    withCredentials([file(credentialsId: 'gateway-wake-env', variable: 'ENV_FILE')]) {
                        sh '''
                            set -e
                            # `cat >`, not `cp`: the credential's copy is mode 0400
                            rm -f release/wake/wake.env
                            cat "$ENV_FILE" > release/wake/wake.env
                            chmod 600 release/wake/wake.env

                            # A container started by hand from another folder belongs to another
                            # compose project and holds the name; replace it
                            owner=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' gateway-wake-front 2>/dev/null || true)
                            if [ -n "$owner" ] && [ "$owner" != "$COMPOSE_PROJECT_NAME" ]; then
                                echo "replacing the gateway-wake-front started from project '$owner'"
                                docker rm -f gateway-wake-front >/dev/null
                            fi

                            export WAKE_IMAGE="${REGISTRY}/${HARBOR_PROJECT}/${WAKE_REPO}:${SELECTED_TAG}"
                            docker compose -f release/wake/docker-compose.yml up -d --no-build --pull never
                            rm -f release/wake/wake.env
                        '''
                    }
                }
            }
        }

        stage('Smoke test') {
            steps {
                timeout(time: 5, unit: 'MINUTES') {
                    sh '''
                        set -e
                        curl -sf -m 5 "$GATEWAY_URL/health" >/dev/null && echo "gateway on homelabai is answering"

                        # The wake front uses host networking; ask it from inside, as deploy-authserver does
                        for i in $(seq 1 20); do
                          status=$(docker exec gateway-wake-front python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9100/__wake/status', timeout=3).read().decode())" 2>/dev/null || true)
                          case "$status" in
                            *'"up":true'*) echo "wake front (tag ${SELECTED_TAG}) is up and sees the gateway"; break ;;
                          esac
                          [ "$i" = 20 ] && { echo "wake front never reported the gateway up: $status" >&2; docker logs --tail 50 gateway-wake-front || true; exit 1; }
                          sleep 3
                        done
                    '''
                }
            }
        }

        stage('Prune old Harbor tags') {
            steps {
                timeout(time: 10, unit: 'MINUTES') {
                    withCredentials([usernamePassword(
                        credentialsId: 'harbor-robot-ai-gateway',
                        usernameVariable: 'HARBOR_USER',
                        passwordVariable: 'HARBOR_PASS'
                    )]) {
                        script {
                            // Best effort: a failed delete never fails a deploy that worked
                            def stale = env.STALE_TAGS ? env.STALE_TAGS.split(',') : []
                            stale.each { tag ->
                                if (!tag || tag == env.SELECTED_TAG) return
                                withEnv(["PRUNE_TAG=${tag}"]) {
                                    sh '''
                                        for repo in "$RELEASE_REPO" "$WAKE_REPO"; do
                                            docker run --rm --network host curlimages/curl -sf -o /dev/null -X DELETE -u "$HARBOR_USER:$HARBOR_PASS" "http://${REGISTRY}/api/v2.0/projects/${HARBOR_PROJECT}/repositories/${repo}/artifacts/${PRUNE_TAG}" || echo "warn: could not delete ${repo}:${PRUNE_TAG} (continuing)"
                                        done
                                    '''
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            // Never leave the settings file behind in the workspace
            sh 'rm -f release/wake/wake.env || true'
        }
        failure {
            echo 'Deploy failed -- see the failing stage above. bin/deploy.sh has already rolled the gateway back if it was that stage. To go back to an earlier version, run this job again and pick the previous tag.'
        }
    }
}
