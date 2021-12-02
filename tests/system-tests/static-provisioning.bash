# ---------------------------------------------------------------------------- #

pav_log "Create PavProvisioner..."

kubectl create -f provisioner.yaml

# ---------------------------------------------------------------------------- #

pav_log "Create PV..."

kubectl create -f pv.yaml

# ---------------------------------------------------------------------------- #

pav_log "Create PVC and wait until it is bound..."

kubectl create -f pvc-static.yaml

# shellcheck disable=SC2016
pav_poll 1 60 '[[
    "$( kubectl get pvc my-pvc -o=jsonpath="{.status.phase}" )" = Bound
    ]]'

# ---------------------------------------------------------------------------- #

pav_log "Create Pod and wait until it terminates..."

kubectl create -f pod.yaml

# shellcheck disable=SC2016
pav_poll 1 60 '[[
    "$( kubectl get pod my-pod -o=jsonpath="{.status.phase}" )" =~ \
    ^Succeeded|Failed$
    ]]'

[[ "$( kubectl get pod my-pod -o=jsonpath="{.status.phase}" )" = Succeeded ]]

# ---------------------------------------------------------------------------- #

pav_log "Delete Pod..."

kubectl delete pod my-pod --timeout=60s

# ---------------------------------------------------------------------------- #

pav_log "Delete PVC..."

kubectl delete pvc my-pvc --timeout=60s

# ---------------------------------------------------------------------------- #

pav_log "Delete PavProvisioner..."

kubectl delete pav my-provisioner --timeout=60s

# ---------------------------------------------------------------------------- #
