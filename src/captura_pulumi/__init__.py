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

    # Create traefik.

    traefik = (
        cluster.id.apply(lambda id: k8s.k8s.Provider("k8s-provider", cluster=id))
        .apply(lambda _: k8s.create_traefik(config))
        .apply(lambda _: k8s.create_error_pages(config))
        .apply(lambda _: k8s.create_traefik_ingressroutes(config))
        # .apply(lambda _: k8s.create_captura(config))
    )

    # NOTE: Because stupid object storage sucks or I suck at using it.
    if config.require_bool("registry") is True:
        bucket, bucket_key = linode.create_bucket(config)
        pulumi.Output.all(
            traefik,
            bucket_key.access_key,
            bucket_key.secret_key,
            bucket.cluster,
            bucket.endpoint,
            bucket.label,
        ).apply(
            lambda data: k8s.create_registry(
                config,
                access_key=data[1],
                secret_key=data[2],
                cluster=data[3],
                endpoint=data[4],
                label=data[5],
            )
        )
