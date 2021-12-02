#!/usr/bin/env bash
# ---------------------------------------------------------------------------- #

set -o errexit -o pipefail -o nounset

# ---------------------------------------------------------------------------- #
# check usage

build_and_push=1
interactive=1
tests=()

for arg in "$@"; do
    case "${arg}" in
        --no-build-and-push)
            build_and_push=0
            ;;
        --no-interactive)
            interactive=0
            ;;
        *)
            if [[ -z "${image+set}" ]]; then
                image="${arg}"
            else
                tests+=( "${arg}" )
            fi
            ;;
    esac
done

image_regex='^([A-Za-z0-9._-]+:[0-9]+/)?([A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+:[A-Za-z0-9._-]+$'

{ [[ "${image:-}" =~ ${image_regex} ]] && (( ${#tests[@]} > 0 )); } || {
    >&2 echo -n "\
Usage: $0 [<options...>] <registry>:<port>/<image>:<tag> <test...>
       $0 [<options...>]                   <image>:<tag> <test...>

Run the given system tests against the active kubectl context.

The first argument is the tag to use when building and pushing the PaV image.
Note that <image> may contain '/' (slashes).

In the first form, the PaV image is built and pushed to the given repository
before running the tests. Note that you must specify a repository port; this is
to avoid pushing to public registries by mistake. In the second form, the PaV
image is built but not pushed.

In either form, adding flag --no-build-and-push will skip building and pushing
the image before running the tests. Also, adding --no-interactive will skip all
prompts.
"
    exit 2
}

for test in "${tests[@]}"; do
    if [[ ! -e "${test}" ]]; then
        >&2 echo "Test file does not exist: ${arg}"
        exit 1
    fi
done

# ---------------------------------------------------------------------------- #
# some private definitions

script_dir="$( realpath -e "$0" | xargs dirname )"
repo_root="$( realpath -e "${script_dir}/.." )"

function _big_log()
{
    local text term_cols sep_len
    text="$( printf "${@:2}" )"
    term_cols="$( tput cols 2> /dev/null )" || term_cols=80
    sep_len="$(( term_cols - ${#text} - 16 ))"
    >&2 printf "\033[%sm--- [%s] %s " "$1" "$( date +%H:%M:%S )" "${text}"
    printf '%*s\033[0m\n' "$(( sep_len < 0 ? 0 : sep_len ))" '' | >&2 tr ' ' -
}

function _info()    { _big_log 33 "$@"; }
function _success() { _big_log 32 "$@"; }
function _error()   { _big_log 31 "$@"; }

# ---------------------------------------------------------------------------- #
# some definitions shared with the test scripts

export PAV_REPO_ROOT="${repo_root}"

function pav_log()
{
    (
        set -o errexit -o nounset +o xtrace

        >&2 printf "\033[36m--- [%s] %s\033[0m\n" \
            "$( date +%H:%M:%S )" "$( printf "$@" )"
    )
}
export -f pav_log

function pav_install()
{
    (
        set -o errexit -o nounset +o xtrace

        pav_log "Installing PaV..."
        sed "s|albertofaria/pav:[0-9+]\.[0-9+]\.[0-9+]|${image}|g" \
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
        kubectl delete --ignore-not-found crd pavprovisioners.pav.albertofaria.github.io
        kubectl delete --ignore-not-found -f "${repo_root}/deployment.yaml"
    )
}
export -f pav_uninstall

function pav_wait_for_input()
{
    (
        set -o errexit -o nounset +o xtrace

        if (( interactive )); then
            read -rp "$(
                printf \
                    "\033[33m[%s]\033[0m \033[33mPress [ENTER] to continue...\033[0m " \
                    "$( date '+%H:%M:%S' )"
                )"
        fi
    )
}
export -f pav_wait_for_input

# Usage: pav_poll <retry_delay> <max_tries> <command>
function pav_poll()
{
    (
        set -o errexit -o nounset +o xtrace

        for (( i = 1; i < "$2"; ++i )); do
            if eval "${*:3}"; then return 0; fi
            sleep "$1"
        done

        if eval "${*:3}"; then return 0; fi

        return 1
    )
}
export -f pav_poll

export -f pav_install
export -f pav_uninstall
export -f pav_wait_for_input
export -f pav_poll

# ---------------------------------------------------------------------------- #
# build and push PaV image

if (( build_and_push )); then

    pav_log "Building PaV image (%s)..." "${image}"
    docker image build --tag="${image}" "${repo_root}/image"

    if [[ "${image}" =~ ^[A-Za-z0-9._-]+:[0-9]+/ ]]; then
        pav_log "Pushing PaV image (%s)..." "${image}"
        docker image push "${image}"
    fi

fi

# ---------------------------------------------------------------------------- #
# run tests

i=0
num_succeeded=0
num_failed=0

canceled=0
trap 'canceled=1' SIGINT

for test in "${tests[@]}"; do

    test_abs="$( realpath -e "${test}" )"
    test_dir="$( dirname "${test_abs}" )"

    _info 'Running test %s (%d of %d)...' "${test}" "$(( ++i ))" "${#tests[@]}"

    set +o errexit

    (
        set -o errexit -o pipefail -o nounset
        cd "${test_dir}"
        pav_install
        set -o xtrace
        # shellcheck disable=SC1090
        source "${test_abs}"
    )

    exit_code="$?"
    set -o errexit

    if (( exit_code != 0 && interactive )); then
        read -rp "$(
            printf "\033[33m--- [%s] %s\033[0m " "$( date '+%H:%M:%S' )" \
                'Test failed, press [ENTER] to clean up and continue...'
            )"
    fi

    (
        set -o errexit -o pipefail -o nounset

        pav_log 'Deleting objects created by test...'
        kubectl delete deployment,job,pav,pod,pv,pvc,sc -l=pav-test-object

        pav_uninstall || {
            >&2 printf "\033[1;31mFailed to uninstall PaV.\033[0m\n"
            exit 1
        }
    )

    if (( canceled )); then break; fi

    if (( exit_code == 0 )); then
        : $(( num_succeeded++ ))
    else
        : $(( num_failed++ ))
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

(( num_succeeded == ${#tests[@]} ))

# ---------------------------------------------------------------------------- #
