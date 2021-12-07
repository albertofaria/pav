# ---------------------------------------------------------------------------- #

for example in "${PAV_REPO_ROOT}/examples/"*; do

    pav_log "Create PavProvisioner from example ${example}..."

    kubectl create -f "${example}/provisioner.yaml"
    kubectl delete -f "${example}/provisioner.yaml" --timeout=60s

done

# ---------------------------------------------------------------------------- #
