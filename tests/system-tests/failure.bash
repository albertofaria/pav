# ---------------------------------------------------------------------------- #

pav_log "Create PavProvisioner..."

kubectl create -f - <<EOF
  apiVersion: pav.albertofaria.github.io/v1alpha1
  kind: PavProvisioner
  metadata:
    name: my-provisioner
  spec:
    provisioningModes:
      - Dynamic
    volumeValidation:
      podTemplate:
        spec:
          restartPolicy: Never
          containers:
            - name: container
              image: alpine:3.15
              command:
                - "{{ pvc.metadata.annotations.validationCommand }}"
    volumeCreation:
      capacity: 1Gi
      podTemplate:
        spec:
          restartPolicy: Never
          containers:
            - name: container
              image: alpine:3.15
              command:
                - "{{ pvc.metadata.annotations.creationCommand }}"
    volumeStaging:
      podTemplate:
        spec:
          restartPolicy: Never
          containers:
            - name: container
              image: alpine:3.15
              command:
                - "{{ pvc.metadata.annotations.stagingCommand }}"
EOF

# ---------------------------------------------------------------------------- #

pav_log "Create StorageClass..."

kubectl create -f sc.yaml

# ---------------------------------------------------------------------------- #

pav_log "Validation failure: Create PVC and ensure that it remains unbound..."

kubectl create -f - <<EOF
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata:
    name: my-pvc
    annotations:
      validationCommand: "false"
      creationCommand:   "true"
      stagingCommand:    "true"
  spec:
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 1Gi
    storageClassName: my-sc
EOF

sleep 60
[[ "$( kubectl get pvc my-pvc -o=jsonpath="{.status.phase}" )" = Pending ]]

kubectl delete pvc my-pvc --timeout=60s

# ---------------------------------------------------------------------------- #

pav_log "Creation failure: Create PVC and ensure that it remains unbound..."

kubectl create -f - <<EOF
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata:
    name: my-pvc
    annotations:
      validationCommand: "true"
      creationCommand:   "false"
      stagingCommand:    "true"
  spec:
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 1Gi
    storageClassName: my-sc
EOF

sleep 60
[[ "$( kubectl get pvc my-pvc -o=jsonpath="{.status.phase}" )" = Pending ]]

kubectl delete pvc my-pvc --timeout=60s

# ---------------------------------------------------------------------------- #

pav_log "Staging failure: Create PVC and Pod and ensure that it can't run..."

kubectl create -f - <<EOF
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata:
    name: my-pvc
    annotations:
      validationCommand: "true"
      creationCommand:   "true"
      stagingCommand:    "false"
  spec:
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 1Gi
    storageClassName: my-sc
EOF

# shellcheck disable=SC2016
pav_poll 1 60 '[[
    "$( kubectl get pvc my-pvc -o=jsonpath="{.status.phase}" )" = Bound
    ]]'

kubectl create -f pod.yaml

sleep 60
[[ "$( kubectl get pod my-pod -o=jsonpath="{.status.phase}" )" = Pending ]]

kubectl delete pod my-pod --timeout=60s
kubectl delete pvc my-pvc --timeout=60s

# ---------------------------------------------------------------------------- #

pav_log "Delete objects..."

kubectl delete sc my-sc --timeout=60s
kubectl delete pav my-provisioner --timeout=60s

# ---------------------------------------------------------------------------- #
