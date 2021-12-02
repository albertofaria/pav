<!-- ----------------------------------------------------------------------- -->

This example is an extended version of the Google Cloud Storage integration showcased in Section 4.1 of the paper [__Pods-as-Volumes: Effortlessly Integrating Storage Systems and Middleware into Kubernetes__, in _Seventh International Workshop on Container Technologies and Container Clouds (WoC '21)_](https://doi.org/10.1145/3493649.3493653).

  - File [`provisioner.yaml`](provisioner.yaml) defines the `PavProvisioner`.

  - File [`usage-dynamic.yaml`](usage-dynamic.yaml) shows how to use the provisioner with dynamically-provisioned volumes, defining (1) a `StorageClass` that references the `PavProvisioner`, (2) a `PersistentVolumeClaim` that causes a GCS bucket to be allocated and is backed by it, and (3) a `Pod` that mounts the `PersistentVolumeClaim` and sleeps forever.

  - File [`usage-static.yaml`](usage-static.yaml) shows how to use the provisioner with statically-provisioned volumes, defining (1) a `PersistentVolume` that references the `PavProvisioner` and is backed by an existing GCS bucket, (2) a `PersistentVolumeClaim` that references the `PersistentVolume`, and (3) a `Pod` that mounts the `PersistentVolumeClaim` and sleeps forever.

<!-- ----------------------------------------------------------------------- -->
