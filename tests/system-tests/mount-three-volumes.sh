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
    volumeCreation:
      capacity: 1Gi
    volumeStaging:
      podTemplate:
        spec:
          restartPolicy: Never
          containers:
            - name: container
              image: alpine:3.15
              command:
                - /bin/bash
                - -c
                - |
                  mkdir /pav/volume &&
                  echo {{ pvc.metadata.name|tobash }} > /pav/volume/pvc-name
EOF

# ---------------------------------------------------------------------------- #

pav_log "Create StorageClass..."

kubectl create -f - <<EOF
  apiVersion: storage.k8s.io/v1
  kind: StorageClass
  metadata:
    name: my-sc
  provisioner: my-provisioner
EOF

# ---------------------------------------------------------------------------- #

pav_log "Create 3 PVCs and wait until they are bound..."

for (( i = 1; i <= 3; ++i )); do

    kubectl create -f - <<EOF
      apiVersion: v1
      kind: PersistentVolumeClaim
      metadata:
        name: my-pvc-$i
      spec:
        accessModes:
          - ReadWriteOnce
        resources:
          requests:
            storage: 1Gi
        storageClassName: my-sc
EOF

done

for (( i = 1; i <= 3; ++i )); do

    # shellcheck disable=SC2016
    pav_poll 1 60 '[[
        "$( kubectl get pvc "my-pvc-$i" -o=jsonpath="{.status.phase}" )" = Bound
        ]]'

done

# ---------------------------------------------------------------------------- #

pav_log "Create Pod and wait until it terminates..."

kubectl create -f - <<'EOF'
  apiVersion: v1
  kind: Pod
  metadata:
    name: my-pod
  spec:
    restartPolicy: Never
    containers:
      - name: container
        image: alpine:3.15
        command:
          - /bin/bash
          - -c
          - |
            [[ "$( cat /volume-1/pvc-name )" = my-pvc-1 ]] &&
            [[ "$( cat /volume-2/pvc-name )" = my-pvc-2 ]] &&
            [[ "$( cat /volume-3/pvc-name )" = my-pvc-3 ]]
        volumeMounts:
          - { name: volume-1, mountPath: /volume-1 }
          - { name: volume-2, mountPath: /volume-2 }
          - { name: volume-3, mountPath: /volume-3 }
    volumes:
      - { name: volume-1, persistentVolumeClaim: { claimName: my-pvc-1 } }
      - { name: volume-2, persistentVolumeClaim: { claimName: my-pvc-2 } }
      - { name: volume-3, persistentVolumeClaim: { claimName: my-pvc-3 } }
EOF

# shellcheck disable=SC2016
pav_poll 1 60 '[[
    "$( kubectl get pod my-pod -o=jsonpath="{.status.phase}" )" =~ \
    ^Succeeded|Failed$
    ]]'

[[ "$( kubectl get pod my-pod -o=jsonpath="{.status.phase}" )" = Succeeded ]]

# ---------------------------------------------------------------------------- #

pav_log "Delete objects..."

kubectl delete pod my-pod --timeout=60s
kubectl delete pvc my-pvc-3 --timeout=60s
kubectl delete pvc my-pvc-2 --timeout=60s
kubectl delete pvc my-pvc-1 --timeout=60s
kubectl delete sc my-sc --timeout=60s
kubectl delete pav my-provisioner --timeout=60s

# ---------------------------------------------------------------------------- #
