# ---------------------------------------------------------------------------- #

pav_log "Create PavProvisioner..."

kubectl create -f provisioner.yaml

# ---------------------------------------------------------------------------- #

pav_log "Create StorageClass..."

kubectl create -f sc.yaml

# ---------------------------------------------------------------------------- #

pav_log "Create PVC and wait until it is bound..."

kubectl create -f pvc-dynamic.yaml

# shellcheck disable=SC2016
pav_poll 1 60 '[[
    "$( kubectl get pvc my-pvc -o=jsonpath="{.status.phase}" )" = Bound
    ]]'

# ---------------------------------------------------------------------------- #

pav_log "Request PavProvisioner deletion and ensure that it isn't deleted..."

kubectl delete pav my-provisioner --timeout=1s || true

sleep 60
kubectl get pav my-provisioner

# ---------------------------------------------------------------------------- #

pav_log "Ensure that PVC can't be staged..."

kubectl create -f pod.yaml

sleep 60

[[ "$( kubectl get pod my-pod -o=jsonpath="{.status.phase}" )" = Pending ]]

# Kubernetes takes a while to let the pod die waiting for volume mounting to
# succeed, so we use a big timeout
kubectl delete pod my-pod --timeout=120s

# ---------------------------------------------------------------------------- #

pav_log "Create another PVC and ensure that it can't be provisioned..."

yq eval '.metadata.name = "my-pvc-2"' pvc-dynamic.yaml | kubectl create -f -

sleep 60

[[ "$( kubectl get pvc my-pvc-2 -o=jsonpath="{.status.phase}" )" = Pending ]]

# ---------------------------------------------------------------------------- #

pav_log "Delete StorageClass..."

kubectl delete sc my-sc --timeout=60s

# ---------------------------------------------------------------------------- #

pav_log "Ensure that PavProvisioner still exists..."

kubectl get pav my-provisioner

# ---------------------------------------------------------------------------- #

pav_log "Delete PVC..."

kubectl delete pvc my-pvc --timeout=60s

# ---------------------------------------------------------------------------- #

pav_log "Wait until PavProvisioner is deleted..."

# was already called before, only here to wait until actually deleted
kubectl delete pav my-provisioner --timeout=60s

# ---------------------------------------------------------------------------- #
