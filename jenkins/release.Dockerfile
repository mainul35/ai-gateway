# The gateway's code for one commit, as an image, so it can sit in Harbor next to the wake front's
# image under the same tag. Nothing ever runs it: the deploy job copies /src out of it and deploys
# that to homelabai with bin/deploy.sh. Built by jenkins/build-ai-gateway.Jenkinsfile from
# `git archive` of the commit, so it holds exactly what git holds.
FROM scratch
COPY . /src
