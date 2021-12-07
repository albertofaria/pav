#!/usr/bin/env bash
# ---------------------------------------------------------------------------- #

set -o errexit -o pipefail -o nounset

script_dir="$( realpath -e "$0" | xargs dirname )"
repo_root="$( realpath -e "${script_dir}/.." )"

# ---------------------------------------------------------------------------- #
# check usage

fail_fast=0
unset k3s_image
wait_after_failure=0
num_agents=0
tests=()

while (( $# > 0 )); do
    case "$1" in
        --fail-fast)
            fail_fast=1
            ;;
        --k3s-image)
            shift
            k3s_image="$1"
            ;;
        --wait-after-failure)
            wait_after_failure=1
            ;;
        --nodes)
            shift
            num_agents="$(( "$1" - 1 ))"
            ;;
        *)
            tests+=( "$1" )
            ;;
    esac
    shift
done

if (( "${#tests[@]}" == 0 )); then
    >&2 echo -n "\
Usage: $0 [<options...>] <tests...>
       $0 [<options...>] all

Run each given system test against a temporary k3d cluster.

If invoked with a single \`all\` argument, all .bash files under directory
system-tests/ are run as tests.

Options:
   --fail-fast            Cancel remaining tests after a test fails.
   --k3s-image <tag>      Use the given k3s image.
   --nodes                Number of nodes in the cluster, including the control
                          plane node, which can also run pods (default is 1).
   --wait-after-failure   Prompt user for input after a test fails.
"
    exit 2
fi

if (( "${#tests[@]}" == 1 )) && [[ "${tests[0]}" = all ]]; then
    tests=( "${script_dir}"/system-tests/*.bash )
fi

for test in "${tests[@]}"; do
    if [[ ! -e "${test}" ]]; then
        >&2 echo "Test file does not exist: ${test}"
        exit 1
    fi
done

# ---------------------------------------------------------------------------- #
# some private definitions

function _big_log()
{
    local text term_cols sep_len
    text="$( printf "${@:2}" )"
    term_cols="$( tput cols 2> /dev/null )" || term_cols=80
    sep_len="$(( term_cols - ${#text} - 16 ))"
    printf "\033[%sm--- [%s] %s " "$1" "$( date +%H:%M:%S )" "${text}"
    printf '%*s\033[0m\n' "$(( sep_len < 0 ? 0 : sep_len ))" '' | tr ' ' -
}

function _print_error()
{
    printf "\033[36m--- [%s] \033[31m%s\033[0m" \
            "$( date +%H:%M:%S )" "$( printf "$@" )"
}

function _failure_prompt()
{
    _print_error "$1, use the following to point kubectl at the k3d cluster:"
    echo
    _print_error "   export KUBECONFIG=$KUBECONFIG"
    echo
    read -rp "$( _print_error "Press [ENTER] to clean up and continue... " )"
}

# ---------------------------------------------------------------------------- #
# some definitions shared with the test scripts

export PAV_REPO_ROOT="${repo_root}"

function pav_log()
{
    (
        set -o errexit -o nounset +o xtrace

        printf "\033[36m--- [%s] %s\033[0m\n" \
            "$( date +%H:%M:%S )" "$( printf "$@" )"
    )
}
export -f pav_log

function pav_install()
{
    (
        set -o errexit -o nounset +o xtrace

        pav_log "Installing PaV..."
        sed "s|albertofaria/pav:[0-9+]\.[0-9+]\.[0-9+]|pav:test|g" \
            "${repo_root}/deployment.yaml" | kubectl create -f -

        pav_log "Waiting for the controller agent to come up..."
        kubectl --namespace=pav rollout status deployment/controller-agent \
            --timeout=60s
    )
}
export -f pav_install

function pav_uninstall()
{
    (
        set -o errexit -o nounset +o xtrace

        pav_log "Uninstalling PaV..."
        kubectl delete --ignore-not-found --timeout=60s \
            crd pavprovisioners.pav.albertofaria.github.io
        kubectl delete --ignore-not-found --timeout=60s \
            -f "${repo_root}/deployment.yaml"
    )
}
export -f pav_uninstall

# Usage: pav_poll <retry_delay> <max_tries> <command>
function pav_poll()
{
    (
        set -o errexit -o nounset +o xtrace

        for (( __poll_index = 1; __poll_index < "$2"; ++__poll_index )); do
            if eval "${*:3}"; then return 0; fi
            sleep "$1"
        done

        if eval "${*:3}"; then return 0; fi

        return 1
    )
}
export -f pav_poll

# ---------------------------------------------------------------------------- #
# build PaV image

pav_log "Building PaV image (pav:test)..."
docker image build --tag=pav:test "${repo_root}/image"

# ---------------------------------------------------------------------------- #
# create temporary directory

temp_dir="$( mktemp -d )"
export KUBECONFIG="$temp_dir/kubeconfig"

trap '{
    k3d cluster delete pav-test
    rm -fr "$temp_dir" || true
    }' EXIT

function _create_k3d_cluster()
{
    local i volumes=()

    pav_log 'Creating k3d cluster...'

    # We need to mount a volume at /var/lib/kubernetes-pav with bidirectional
    # mount propagation ("shared") for things to work.

    mkdir "$temp_dir/vol-master"
    volumes+=( "$temp_dir/vol-master:/var/lib/kubernetes-pav:shared@server:0" )

    for (( i = 0; i < num_agents; ++i )); do
        mkdir "$temp_dir/vol-$i"
        volumes+=( "$temp_dir/vol-$i:/var/lib/kubernetes-pav:shared@agent:$i" )
    done

    # shellcheck disable=SC2068
    k3d cluster create \
        --agents "${num_agents}" \
        ${k3s_image:+"--image=${k3s_image}"} \
        --kubeconfig-switch-context=false \
        --kubeconfig-update-default=false \
        --no-lb \
        ${volumes[@]/#/--volume } \
        pav-test

    k3d kubeconfig get pav-test > "$temp_dir/kubeconfig"
}

function _delete_k3d_cluster()
{
    local i

    pav_log 'Deleting k3d cluster...'

    k3d cluster delete pav-test

    for (( i = 0; i < num_agents; ++i )); do
        rm -fr "$temp_dir/vol-$i" || true
    done

    rm -fr "$temp_dir/vol-master" || true

    rm -f "$temp_dir/kubeconfig"
}

# ---------------------------------------------------------------------------- #
# run tests

test_i=0
num_succeeded=0
num_failed=0

canceled=0
trap 'canceled=1' SIGINT

for test in "${tests[@]}"; do

    test_abs="$( realpath -e "${test}" )"
    test_dir="$( dirname "${test_abs}" )"

    _big_log 33 'Running test %s (%d of %d)...' \
        "${test}" "$(( ++test_i ))" "${#tests[@]}"

    _create_k3d_cluster

    pav_log 'Importing PaV image into k3d cluster...'
    k3d image import --cluster=pav-test --mode=direct pav:test

    set +o errexit

    (
        set -o errexit -o pipefail -o nounset -o xtrace

        pav_install

        cd "${test_dir}"
        # shellcheck disable=SC1090
        source "${test_abs}"
    )

    exit_code="$?"
    set -o errexit

    if (( exit_code == 0 )); then

        pav_uninstall || exit_code="$?"

        if (( exit_code != 0 )); then
            if (( wait_after_failure )); then
                _failure_prompt 'Failed to uninstall PaV'
            else
                _print_error 'Failed to uninstall PaV.'
                echo
            fi
        fi

    else

        if (( wait_after_failure )); then
            _failure_prompt 'Test failed'
        else
            _print_error 'Test failed.'
            echo
        fi

    fi

    _delete_k3d_cluster

    if (( canceled )); then
        break
    elif (( exit_code == 0 )); then
        : $(( num_succeeded++ ))
    else
        : $(( num_failed++ ))
        if (( fail_fast )); then break; fi
    fi

done

# ---------------------------------------------------------------------------- #
# print summary

num_canceled="$(( ${#tests[@]} - num_succeeded - num_failed ))"

if (( num_failed > 0 )); then
    color=31
elif (( num_canceled > 0 )); then
    color=33
else
    color=32
fi

_big_log "${color}" '%d succeeded, %d failed, %d canceled' \
    "${num_succeeded}" "${num_failed}" "${num_canceled}"

trap 'rm -fr "$temp_dir" || true' EXIT

(( num_succeeded == ${#tests[@]} ))

# ---------------------------------------------------------------------------- #
