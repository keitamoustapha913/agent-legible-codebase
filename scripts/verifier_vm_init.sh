#!/bin/sh
# Install as /init in a reviewed, pinned verifier initramfs.
# Load the image's matching virtio PCI/block and ext2 drivers before mounting vda.
set -eu
PATH=/usr/local/bin:/usr/bin:/bin:/sbin
export PATH
trap 'poweroff -f' EXIT
mkdir -p /dev /proc /sys /run /tmp
mount -t devtmpfs devtmpfs /dev
mount -t proc proc /proc
mount -t sysfs sysfs /sys
chmod 1777 /tmp
chmod 0600 /dev/ttyS0 /dev/console
if [ -e /dev/kmsg ]; then
    chmod 0600 /dev/kmsg
fi
mkdir -m 0700 /run/alc-private
mkdir /run/alc-private/input
mount -t ext2 -o ro,nodev,nosuid,noexec /dev/vda /run/alc-private/input
chmod 0600 /dev/vda
/usr/bin/python3 -B -E -s /run/alc-private/input/verifier_guest.py /run/alc-private/input
