#!/bin/bash -x

set -o errexit
set -o nounset
set -o pipefail

dnf update -y
dnf install wget zip unzip -y
dnf install epel-release -y
dnf install qemu-kvm libvirt virt-install -y
# dnf groupinstall "Virtualization Host" -y

rm -rf /usr/sbin/packer

yum-config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo
dnf install packer -y