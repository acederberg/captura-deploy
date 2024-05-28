"""
Useful links

  .. code:: txt

    [1] LKE Firewall:                        https://www.linode.com/docs/products/compute/kubernetes/get-started/#general-network-and-firewall-information
    [2] LKE Not added to `captura-firewall`: https://www.linode.com/community/questions/19155/securing-k8s-cluster
    [3] Node types:                          https://api.linode.com/v4/linode/types
"""

# =========================================================================== #
import itertools
from collections.abc import Sequence
from typing import Tuple

import pulumi
import pulumi_linode as linode
from pulumi.output import Output

# NOTE: Not going to bother writing out an actual configuration for now.
#       To learn more do ``linode-cli linode types``.
CLUSTER_K8S_VERSION = "1.29"
CLUSTER_DEFAULT_NODE_COUNT = 1
CLUSTER_NODE_TYPE = "g6-standard-1"
CLUSTER_TAGS = ["captura", "dev"]
CLUSTER_REGION = "us-west"

# NOTE: Options for these regions can be found in https://www.linode.com/docs/products/storage/object-storage/#availability
OBJECT_STORAGE_CLUSTER = "us-lax-1"


# --------------------------------------------------------------------------- #
# NOTE: Create a Linode Kubernetes cluster


def create_cluster_firewall_device(
    config: pulumi.Config,
    *,
    pools: Output[Sequence[linode.LkeNodePool]],
    id_firewall: int,
):
    """Add pool nodes to the firewal."""
    tuple(
        map(
            lambda node: linode.FirewallDevice(
                "captura-firewall-cluster-device",
                entity_id=int(node["instance_id"]),
                firewall_id=int(id_firewall),
                entity_type="linode",
            ),
            itertools.chain(*(p["nodes"] for p in pools)),
        )
    )


def create_cluster(config: pulumi.Config) -> Tuple[linode.LkeCluster, linode.Firewall]:
    cluster = linode.LkeCluster(
        "captura-cluster",
        linode.LkeClusterArgs(
            label="captura-cluster",
            k8s_version=CLUSTER_K8S_VERSION,
            region=CLUSTER_REGION,
            pools=[
                linode.LkeClusterPoolArgs(
                    count=CLUSTER_DEFAULT_NODE_COUNT,
                    type=CLUSTER_NODE_TYPE,
                )
            ],
            tags=CLUSTER_TAGS,
        ),
    )

    # NOTE: This should make it such that all devices attached cannot be accessed
    #       using ssh directly. Somehow linode made it such that using lsh ssh is
    #       still possible.
    # NOTE: Enum values for fields are no always available through Pulumi docs, so
    #       instead see the api docs:
    #       https://www.linode.com/docs/api/networking/#firewall-rules-update
    firewall = linode.Firewall(
        "captura-firewall",
        linode.FirewallArgs(
            label="captura-firewall-k8s",
            inbound_policy="DROP",
            inbounds=[
                # NOTE: For kublete health checks, wiregaurd tunneling, calico, and
                #       node ports.
                linode.FirewallInboundArgs(
                    label="captura-firewall-internal",
                    action="ACCEPT",
                    protocol="TCP",
                    ports="179, 10250, 30000-32767",
                    ipv4s=["192.168.128.0/17"],
                ),
                linode.FirewallInboundArgs(
                    label="captura-firewall-internal-udp",
                    action="ACCEPT",
                    protocol="UDP",
                    ports="51820, 30000-32767",
                    ipv4s=["192.168.128.0/17"],
                ),
                linode.FirewallInboundArgs(
                    label="captura-firewall-imap-out",
                    action="ACCEPT",
                    protocol="IPENCAP",
                    ipv4s=["192.168.128.0/17"],
                ),
            ],
            # NOTE: Tried setting these to no avail - in the forum post [2] the
            #       comment from stravostino says to allow all outbound.
            outbound_policy="ACCEPT",
            outbounds=[],
            tags=CLUSTER_TAGS,
        ),
    )

    pulumi.Output.all(pools=cluster.pools, id_firewall=firewall.id).apply(
        lambda args: create_cluster_firewall_device(
            config,
            pools=args["pools"],
            id_firewall=args["id_firewall"],
        )
    )

    return cluster, firewall


def create_bucket(
    config: pulumi.Config,
) -> Tuple[
    linode.ObjectStorageBucket,
    linode.ObjectStorageKey,
]:
    # NOTE: Keyword argument ``cluster`` is not the kubernetes cluster. It is
    #       instead the region in which the bucket is to exist, which is not
    #       confusing at all lol.
    bucket_name = "captura-object-storage"
    bucket = linode.ObjectStorageBucket(
        bucket_name,
        linode.ObjectStorageBucketArgs(
            label="captura-object-storage",
            cluster=OBJECT_STORAGE_CLUSTER,
        ),
    )
    bucket_key = linode.ObjectStorageKey(
        "captura-object-storage",
        linode.ObjectStorageKeyArgs(
            label="captura-object-storage",
            bucket_accesses=[
                linode.ObjectStorageKeyBucketAccessArgs(
                    bucket_name=bucket_name,
                    cluster=OBJECT_STORAGE_CLUSTER,
                    permissions="read_write",
                )
            ],
        ),
        opts=pulumi.ResourceOptions(depends_on=bucket),
    )
    return bucket, bucket_key
