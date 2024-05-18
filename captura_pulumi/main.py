# =========================================================================== #


# NOTE: DO NOT ADD MAIN! PULUMI SUPPORT FOR PYTHON MODULES IS TRASH! SEE https://github.com/pulumi/pulumi/issues/7360
import k8s
import linode
import pulumi

__version__ = "0.0.0"


def create_captura():
    config = pulumi.Config()

    # Create cluster an buckets.
    cluster, _ = linode.create_cluster(config)
    _ = linode.create_bucket(config)

    # Create traefik.
    cluster.id.apply(
        lambda id_cluster: k8s.create_traefik(config, id_cluster=id_cluster)
    )


create_captura()
