# ---------------------------------------------------------------------------- #

for example in "${PAV_REPO_ROOT}/examples/"*; do

    pav_log "Create PavProvisioner from example ${example}..."

    yq eval '.metadata.labels.pav-test-obj = ""' "${example}/provisioner.yaml" |
        kubectl create -f -

    kubectl delete -f "${example}/provisioner.yaml"

done

# ---------------------------------------------------------------------------- #
