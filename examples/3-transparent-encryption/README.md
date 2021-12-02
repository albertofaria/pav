<!-- ----------------------------------------------------------------------- -->

This example is an extended version of the transparent encryption middleware showcased in Section 4.2 of the paper [__Pods-as-Volumes: Effortlessly Integrating Storage Systems and Middleware into Kubernetes__, in _Seventh International Workshop on Container Technologies and Container Clouds (WoC '21)_](https://doi.org/10.1145/3493649.3493653).

  - File [`provisioner.yaml`](provisioner.yaml) defines the `PavProvisioner`.

  - File [`usage.yaml`](usage.yaml) shows how to use the provisioner, defining (1) a `StorageClass` that references the `PavProvisioner`, (2) a `Secret` storing the encryption passphrase, (3) a "wrapping" `PersistentVolumeClaim` that uses the `StorageClass` and adds encryption to an existing "underlying" `PersistentVolumeClaim`, and (4) a `Pod` that mounts the "wrapping" `PersistentVolumeClaim` and sleeps forever.

Note that creating the wrapping `PersistentVolumeClaim` __will cause all data on the underlying `PersistentVolumeClaim` to be lost!__
The same occurs when deleting the wrapping `PersistentVolumeClaim`.

<!-- ----------------------------------------------------------------------- -->
