#!/bin/bash -x

set -o errexit
set -o nounset
set -o pipefail

packer plugins install github.com/hashicorp/qemu
packer plugins install github.com/hashicorp/ansible
