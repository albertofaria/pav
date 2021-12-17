<!-- ----------------------------------------------------------------------- -->

<h1 align="center">
  PaV: Pods-as-Volumes
  <br>
  <!-- build status of branch main -->
  <a href="https://github.com/albertofaria/pav/actions">
    <img src="https://github.com/albertofaria/pav/workflows/build/badge.svg?branch=main" />
  </a>
  <!-- latest release as of current commit -->
  <a href="https://github.com/albertofaria/pav/releases">
    <img src="https://img.shields.io/badge/version-0.1.1-yellow.svg" />
  </a>
  <!-- license -->
  <a href="LICENSE.txt">
    <img src="https://img.shields.io/badge/license-MIT-blue.svg" />
  </a>
</h1>

PaV, short for _Pods-as-Volumes_, is a Kubernetes plugin that simplifies the implementation of volume provisioners.
It enables you to specify all logic underlying the lifecycle and behavior of volumes as __pod templates__, which are then instantiated as needed to create, delete, and expose volumes to client pods.

PaV can be used to integrate storage systems into Kubernetes and to create storage middleware components (see the [Google Cloud Storage](examples/2-google-cloud-storage) and [transparent encryption middleware](examples/3-transparent-encryption) examples).
It may be seen as a simpler-to-use alternative to the [Container Storage Interface (CSI)](https://github.com/container-storage-interface/spec).

For more information on PaV's applicability and design, please see the paper [__Pods-as-Volumes: Effortlessly Integrating Storage Systems and Middleware into Kubernetes__, in _Seventh International Workshop on Container Technologies and Container Clouds (WoC '21)_](https://doi.org/10.1145/3493649.3493653).

<!-- ----------------------------------------------------------------------- -->

## Table of contents

- [Installation](#installation)
- [Creating provisioners](#creating-provisioners)
  - [`PavProvisioner` objects](#pavprovisioner-objects)
  - [Volume lifecycle](#volume-lifecycle)
    - [Volume validation, creation, and deletion](#volume-validation-creation-and-deletion)
    - [Volume staging and unstaging](#volume-staging-and-unstaging)
  - [Jinja templating](#jinja-templating)
    - [Evaluation context](#evaluation-context)
- [Using provisioners](#using-provisioners)
- [Versioning](#versioning)

<!-- ----------------------------------------------------------------------- -->

## Installation

To install PaV onto a Kubernetes cluster, run:

```console
kubectl create -f https://raw.githubusercontent.com/albertofaria/pav/v0.1.1/deployment.yaml
```

It can take a few seconds for PaV's components to start running, during which time the creation of [`PavProvisioner` objects](#pavprovisioner-objects) will fail with a "connection refused" error.
You can wait for PaV to become ready by running `kubectl -n=pav rollout status deployment/controller-agent`.

To uninstall PaV from a Kubernetes cluster, run:

```console
kubectl delete crd pavprovisioners.pav.albertofaria.github.io
kubectl delete --ignore-not-found -f https://raw.githubusercontent.com/albertofaria/pav/v0.1.1/deployment.yaml
```

The first command will cause the deletion of all existing `PavProvisioner` objects and block until they are fully removed.
Only then may the second command be run safely.

<!-- ----------------------------------------------------------------------- -->

## Creating provisioners

This section describes how PaV can be used to implement new volume provisioners.

This documentation is a work in progress.
For more details, please refer to the paper [__Pods-as-Volumes: Effortlessly Integrating Storage Systems and Middleware into Kubernetes__, in _Seventh International Workshop on Container Technologies and Container Clouds (WoC '21)_](https://doi.org/10.1145/3493649.3493653).

<!-- ----------------------------------------------------------------------- -->

### `PavProvisioner` objects

PaV provides a [custom resource](https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources) named `PavProvisioner`.
Each object of this resource implements a new volume provisioner, and defines the logic to create and delete volumes, as well as to make those volumes available to client pods.
This logic is specified as templates of pod definitions, which are instantiated automatically by PaV when needed.
These provisioners can then be used just like built-in Kubernetes provisioners.
See [Using provisioners](#using-provisioners) for more information.

`PavProvisioner` objects are [cluster-wide](https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/#not-all-objects-are-in-a-namespace), _i.e._, do not belong to any namespace.
Their names must be DNS labels (just like namespace names): contain only lowercase alphanumeric characters or `-`, start and end with alphanumeric characters, and be at most 63 characters long.
Their definitions must follow this general schema, most fields being optional:

```yaml
apiVersion: pav.albertofaria.github.io/v1alpha1
kind: PavProvisioner
metadata: ...
spec:
  provisioningModes: ...
  volumeValidation:
    volumeModes: ...
    accessModes: ...
    minCapacity: ...
    maxCapacity: ...
    podTemplate: ...
  volumeCreation:
    handle: ...
    capacity: ...
    podTemplate: ...
  volumeDeletion:
    podTemplate: ...
  volumeStaging:
    podTemplate: ...
  volumeUnstaging:
    podTemplate: ...
```

The following is a description of the fields that may be specified under `spec`.
Note that all fields but `provisioningModes` can be parameterized for each particular volume using Jinja templates; see [Jinja templating](#jinja-templating) below for more information.

  - **`provisioningModes`, list of string, mandatory.**
    The provisioning modes supported by the provisioner.
    Valid elements are `Dynamic` and `Static`.

  - **`volumeValidation`, object, optional.**
    Accepts the following fields:

    - **`volumeModes`, list of string, optional.**
      The volume modes of the volumes that the provisioner can provision.
      Valid elements are `Filesystem` and `Block`.
      Default is `[Filesystem]`.

    - **`accessModes`, list of string, optional.**
      The access modes that volumes provisioned by the provisioner support.
      Valid elements are `ReadWriteOnce`, `ReadOnlyMany`, and `ReadWriteMany`.
      Default is all three.

    - **`minCapacity`, [capacity](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#meaning-of-memory), optional.**
      The minimum capacity that volumes provisioned by the provisioner may have.
      For dynamic (static) provisioning, this corresponds to the minimum capacity that users may specify in `pvc.spec.resources.limits.storage` (`pv.spec.capacity.storage`).
      Default is to have no minimum capacity requirement.

    - **`maxCapacity`, [capacity](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#meaning-of-memory), optional.**
      The maximum capacity that volumes provisioned by the provisioner may have.
      For dynamic (static) provisioning, this corresponds to the maximum capacity that users may specify in `pvc.spec.resources.requests.storage` (`pv.spec.capacity.storage`).
      Default is to have no maximum capacity requirement.

    - **`podTemplate`, [PodTemplateSpec](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#podtemplatespec-v1-core), optional.**
      The definition of the _volume validation pod_, which will be instantiated and run to completion to validate the requested configuration of a volume that is being created as part of dynamic provisioning.
      This can be useful if the other fields under `volumeValidation` are not expressive enough to validate what you want to validate.

  - **`volumeCreation`, object, optional.**
    May only be specified if `provisioningModes` contains `Dynamic`.
    Accepts the following fields:

    - **`handle`, string, optional.**
      The volume's handle.
      Must be something that is valid as the value of `PersistentVolume.spec.csi.volumeHandle`.
      If not specified, then a value exported by the volume creation pod in file `/pav/handle` is used.
      If the pod also doesn't export it, it is set to `pvc-{uid_of_the_pvc_that_triggered_provisioning}`.

    - **`capacity`, [capacity](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#meaning-of-memory), optional.**
      The volume's capacity.
      If not specified, the volume creation pod must export this value in file `/pav/capacity`.

    - **`podTemplate`, [PodTemplateSpec](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#podtemplatespec-v1-core), optional.**
      The definition of the _volume creation pod_, which will be instantiated and run to completion to satisfy each request to create a volume, as part of dynamic provisioning.
      This field may be omitted if no action needs to be taken for the volume to be created.

  - **`volumeDeletion`, object, optional.**
    May only be specified if `provisioningModes` contains `Dynamic`.
    Accepts the following fields:

    - **`podTemplate`, [PodTemplateSpec](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#podtemplatespec-v1-core), optional.**
      The definition of the _volume deletion pod_, which will be instantiated and run to completion to satisfy each request to delete a dynamically provisioned volume.
      If this fails, manual intervention will be necessary to fully delete the volume and its respective `PersistentVolume` and `PersistentVolumeClaim` objects.
      This field may be omitted if no action needs to be taken for the volume to be deleted.

  - **`volumeStaging`, object, mandatory.**
    Accepts the following fields:

    - **`podTemplate`, [PodTemplateSpec](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#podtemplatespec-v1-core), mandatory.**
      The definition of the _volume staging pod_, which will be instantiated when a volume must be made available on a given node.
      It may terminate after making the volume available or continue running if necessary (more details below).

  - **`volumeUnstaging`, object, optional.**
    Accepts the following fields:

    - **`podTemplate`, [PodTemplateSpec](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#podtemplatespec-v1-core), optional.**
      The definition of the _volume unstaging pod_, which will be instantiated and run to completion to satisfy each request to unstage a volume.
      If this fails, manual intervention will be necessary to fully unstage the volume.
      This field may be omitted if no action needs to be taken for the volume to be unstaged.

<!-- ----------------------------------------------------------------------- -->

### Volume lifecycle

PaV supports both [dynamic](https://kubernetes.io/docs/concepts/storage/dynamic-provisioning/) and [static](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#static) provisioning:

  - Dynamic provisioning occurs when a user creates a `PersistentVolumeClaim` that references a `StorageClass` whose `provisioner` field is set to the name of a `PavProvisioner`;

  - Static provisioning occurs when a user directly creates a `PersistentVolume` with field `spec.csi.driver` set to the name of a `PavProvisioner`.

#### Volume validation, creation, and deletion

When the dynamic provisioning of a volume is triggered (through the creation of a `PersistentVolumeClaim`), PaV begins by _validating_ the requested properties of the volume, comparing those with what was specified under the `spec.volumeValidation` field of the appropriate `PavProvisioner`.
In addition, if a _volume validation pod_ was specified in `spec.volumeValidation.podTemplate`, then it is instantiated and PaV waits until it terminates.
If it terminates in failure, the requested volume configuration is considered to be invalid and provisioning of the volume as a whole fails.

Otherwise, if volume validation concludes successfully, PaV initiates volume _creation_.
It instantiates the _volume creation pod_ specified in `spec.volumeCreation.podTemplate` (if there is one) and waits until it terminates.
If it terminates in failure, volume provisioning fails.
Otherwise, the volume is assumed to have been created and volume provisioning succeeds, the `PersistentVolumeClaim` becoming bound to a a new `PersistentVolume` object representing the volume.

> Kubernetes continually retries volume provisioning while the `PersistentVolumeClaim` exists, and the above process of volume validation and creation will thus be repeated if it fails.

Conversely, when deletion of the `PersistentVolumeClaim` is requested, PaV performs volume _deletion_, instantiating the _volume deletion pod_ specified in `spec.volumeDeletion.podTemplate` (if there is one) and waiting until it terminates.
If it terminates successfully, the volume is assumed to have been deleted and removal of the corresponding `PersistentVolumeClaim` and `PersistentVolume` proceeds.
However, if the _volume deletion pod_ fails, the volume will become stuck in a deleting state and manual intervention will be necessary to fully delete the volume and its respective `PersistentVolume` and `PersistentVolumeClaim` objects.

Additionally, if the _volume creation pod_ terminates in failure, PaV subsequently runs the _volume deletion pod_ (if any) to ensure that any resources allocated or changes made by the former are reverted.

Note that this workflow of volume validation, creation, and deletion is only performed for dynamically-provisioned volumes.
The management of resources underlying statically-provisioned volumes is of the responsibility of the user that creates the corresponding `PersistentVolume` object.

#### Volume staging and unstaging

Whenever a pod that uses a `PersistentVolumeClaim` corresponding to a PaV volume is scheduled to run, PaV performs volume _staging_, which is the process of making that volume available to the client pod in the node that it will run on.
This occurs both for dynamically- and statically-provisioned volumes.
To accomplish this, PaV instantiates the _volume staging pod_ specified in `spec.volumeStaging.podTemplate` and schedules it to the same node as the client pod.
This staging pod may either run to completion or create a file at `/pav/ready` and continue running if necessary.
In either case, it must make the volume available at `/pav/volume` as a directory (if the volume is a file system volume) or block special file (if it is a block volume).
If the staging pod terminates in failure, volume staging as a whole fails and the pod that requested access to the volume does not run (Kubernetes may decide to retry volume staging in this case).

> We call this process "staging" instead of the more common "mounting" to avoid ambiguity with file system mounting, which does not occur when staging block volumes.

When the client pod using the volume terminates, PaV performs volume _unstaging_, stopping execution of the _volume staging pod_ (if it is still running) and then instantiating the _volume unstaging pod_ if it was specified in `spec.volumeUnstaging.podTemplate` (scheduling it to the same node as the client pod and the staging pod) and waiting until it terminates.
If it terminates successfully, the effects of volume staging are assumed to have been fully reverted and termination of the client pod proceeds.
However, if the _volume unstaging pod_ fails, the volume will become stuck in an unstaging state and manual intervention will be necessary to fully unstage the volume and allow the client pod that requested access to it to fully terminate.

Additionally, if the _volume staging pod_ terminates in failure, PaV subsequently runs the _volume unstaging pod_ (if any) on the same node to ensure that any resources allocated or changes made by the former are reverted.

<!-- ----------------------------------------------------------------------- -->

### Jinja templating

In a `PavProvisioner` object, all string fields under `spec` (no matter how nested but with the exception of `provisioningModes`) are evaluated as [Jinja 3.0](https://palletsprojects.com/p/jinja) templates.
Expressions in those templates must evaluate to strings or numeric values, and the templates as a whole evaluate to a string.
(To include a literal `{{`, `{%`, or `{#` in a string field, use Jinja [escaping](https://jinja.palletsprojects.com/en/3.0.x/templates/#escaping), *e.g.*, `{{ '{{' }}`.)

If a template sets the `yaml` variable to `true` (such as by including the statement `{% set yaml = true %}`), then the final string resulting from the template's evaluation is parsed as YAML, and the field holding the template takes on the resulting value.
Note that template evaluation is not recursive: if the resulting value has string fields, they are not evaluated as templates.
When using this feature, applying Jinja's [`|tojson`](https://jinja.palletsprojects.com/en/3.0.x/templates/#jinja-filters.tojson) filter to expressions may be useful to ensure that they mix predictably with surrounding YAML.
Note that JSON generated by this filter never includes newline characters.

PaV also makes a `|tobash` filter available, which escapes a string or numeric value so that it doesn't contain newlines and is interpreted as a single token by Bash or compatible shells.
It encodes newline characters using [ANSI-C quoting](https://www.gnu.org/software/bash/manual/bash.html#ANSI_002dC-Quoting).

A `get_pvc(name, namespace)` function is also provided, which looks up the `PersistentVolumeClaim` object with the given name and namespace.

Contiguous whitespace from the beginning of a line to the start of a statement block will be stripped, as will a trailing newline immediately after the block, and thus lines consisting entirely of a single statement block (possibly prefixed by any amount of whitespace) will completely disappear from the result of evaluating the template.

#### Evaluation context

Fields `volumeValidation`, `volumeCreation`, `volumeDeletion`, `volumeStaging`, and `volumeUnstaging` are evaluated as a whole whenever volumes need to be validated, created, deleted, staged, or unstaged.
Jinja templates (recursively) under these fields are evaluated with a given context, _i.e._, set of variables that they can access.
These contexts are described here.

Field `volumeValidation` (and subfields) is evaluated every time (1) a volume is being dynamically provisioned or (2) a statically provisioned volume is being staged, and with the following context variables:

> Volume validation for statically-provisioned volumes is not yet implemented.

  - `requestedVolumeMode` (string): the mode (_i.e._, `Filesystem` or `Block`) that was requested for the volume (specified in `pvc.spec.volumeMode` for dynamic provisioning, and in `pv.spec.volumeMode` for static provisioning);

  - `requestedAccessModes` (list of string): the access modes that were requested to be supported by the volume (specified in `pvc.spec.accessModes` for dynamic provisioning, and in `pv.spec.accessModes` for static provisioning);

  - `requestedMinCapacity` (integer): the volume's requested minimum capacity, in bytes (specified in `pvc.spec.resources.requests.storage` for dynamic provisioning, and in `pv.spec.capacity.storage` for static provisioning);

  - `requestedMaxCapacity` (integer or `null`): the volume's requested maximum capacity, in bytes (specified in `pvc.spec.resources.limits.storage` for dynamic provisioning (defaulting to `null`), and in `pv.spec.capacity.storage` for static provisioning);

  - `params` (object mapping strings to strings): the parameters specified in `sc.parameters` (for dynamic provisioning) or in `pv.spec.csi.volumeAttributes` (for static provisioning);

  - `handle` (string, *only present for static provisioning*): alias for `pv.spec.csi.volumeHandle`;

  - `sc` ([StorageClass](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#storageclass-v1-storage-k8s-io), *only present for dynamic provisioning*): the `StorageClass` object of the `PersistentVolumeClaim` that triggered the provisioning (identified by `pvc.spec.storageClassName`), as returned by the Kubernetes API server _at the time provisioning was triggered_;

  - `pvc` ([PersistentVolumeClaim](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#persistentvolumeclaim-v1-core), *only present for dynamic provisioning*): the `PersistentVolumeClaim` object that triggered the provisioning, as returned by the Kubernetes API server _at the time the template was being evaluated_;

  - `pv` ([PersistentVolume](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#persistentvolume-v1-core), *only present for static provisioning*): the `PersistentVolume` object of the volume in question, as returned by the Kubernetes API server _at the time the template was being evaluated_.

Fields `volumeCreation` and `volumeDeletion` (and subfields) are evaluated every time a volume is respectively being created or deleted (*dynamic provisioning only*), and with the following context variables:

  - (The context for `volumeValidation`.
    Note that `sc` and `pvc` are guaranteed to be present here, and `pv` and `handle` to be absent.)

  - `defaultHandle` (string): the handle that will be attributed to the volume if none is specified under `spec.volumeCreation.handle` and by the volume creation pod (if any) in file `/pav/handle`.
    This has the value `pvc-{uid_of_the_pvc_that_triggered_provisioning}`.

Fields `volumeStaging` and `volumeUnstaging` (and subfields) are evaluated every time a volume is respectively being staged or unstaged, and with the following context variables:

  - `volumeMode` (string): the mode (_i.e._, `Filesystem` or `Block`) of the volume (same as `pvc.spec.volumeMode`);

  - `accessModes` (list of string): the access modes supported _by the `PersistentVolumeClaim`_ being used to stage the volume (same as `pvc.spec.accessModes`), which may be a subset of the access modes actually supported by the volume;

  - `capacity` (integer): the volume's capacity, in bytes (same as `pv.spec.capacity.storage`, but an integer and guaranteed to be in bytes);

  - `params` (object mapping strings to strings): the parameters specified in `pv.spec.csi.volumeAttributes` (which in the case of dynamic provisioning were obtained from the `StorageClass`);

  - `handle` (string): alias for `pv.spec.csi.volumeHandle`;

  - `readOnly` (boolean): whether the volume should be staged in read-only mode (as opposed to read-write mode);

  - `pvc` ([PersistentVolumeClaim](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#persistentvolumeclaim-v1-core)): the `PersistentVolumeClaim` object through which the volume is being staged/unstaged, as returned by the Kubernetes API server at the time the template was being evaluated;

  - `pv` ([PersistentVolume](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#persistentvolume-v1-core)): the `PersistentVolume` object of the volume in question, as returned by the Kubernetes API server at the time the template was being evaluated;

  - `node` ([Node](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.22/#node-v1-core)): the `Node` object corresponding to the node on which the volume is being staged/unstaged, as returned by the Kubernetes API server at the time the template was being evaluated.

<!-- ----------------------------------------------------------------------- -->

## Using provisioners

Volume provisioners implemented using PaV are used in the same manner as other provisioners, by specifying their name in the `provisioner` field of a `StorageClass` (dynamic provisioning) or in the `spec.csi.driver` field of a `PersistentVolume` (static provisioning).
For now, please refer to the [examples/](examples/) for more details.

<!-- ----------------------------------------------------------------------- -->

## Versioning

There are two version numbers: (1) the PaV version and (2) the `pav.albertofaria.github.io` Kubernetes API group version.

The PaV version follows the [SemVer](https://semver.org/spec/v2.0.0.html) scheme, and is currently of the format `0.x.y`, where `x` and `y` are integers corresponding to PaV's _minor version_ and _patch version_, respectively.
The API group version is used for `PavProvisioner` objects, and is currently of the format `v1alphaN`, where `N` is an integer (thus `apiVersion: pav.albertofaria.github.io/v1alphaN`).

When creating a new release of PaV, whenever the `PavProvisioner` schema or semantics change in some backward-compatible way (or if only implementation changes are made), PaV's _patch version_ is incremented and the API group version remains the same.
When they change in some incompatible way, PaV's _minor version_ is incremented and the patch version reset to 0, and the `N` in the API group version is incremented.

Only a single PaV version may be installed in a cluster at any one time, and `PavProvisioner` objects from one API group version cannot be used with a PaV version that uses a different API group version.

<!-- ----------------------------------------------------------------------- -->
