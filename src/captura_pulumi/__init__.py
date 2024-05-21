# =========================================================================== #


import pulumi

# --------------------------------------------------------------------------- #
# NOTE: DO NOT ADD MAIN! PULUMI SUPPORT FOR PYTHON MODULES IS TRASH! SEE https://github.com/pulumi/pulumi/issues/7360
from captura_pulumi import k8s, linode, porkbun

__version__ = "0.0.0"


def create_captura():
    config = pulumi.Config()

    # Create cluster an buckets.
    cluster, _ = linode.create_cluster(config)
    _ = linode.create_bucket(config)

    # Create traefik.
    traefik = cluster.id.apply(
        lambda id_cluster: k8s.create_traefik(config, id_cluster=id_cluster)
    )
    error_pages = traefik.namespace.apply(
        lambda ns: k8s.create_error_pages(config, namespace=ns)
    )
