<!-- ----------------------------------------------------------------------- -->

This example implements a simple provisioner that dynamically allocates file system volumes containing a single `greeting` file with contents `Hello world!`.

  - File [`provisioner.yaml`](provisioner.yaml) defines the `PavProvisioner`.

  - File [`usage.yaml`](usage.yaml) shows how to use the provisioner, defining (1) a `StorageClass` that references the `PavProvisioner`, (2) a `PersistentVolumeClaim` that uses the `StorageClass`, and (3) a `Pod` that mounts the `PersistentVolumeClaim` and sleeps forever.

<!-- ----------------------------------------------------------------------- -->
