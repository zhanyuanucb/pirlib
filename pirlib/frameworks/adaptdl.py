from pirlib.pir import Framework


class AdaptDL(Framework):
    def __init__(self, min_replicas=None, max_replicas=None):
        config = {
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
        }
        super().__init__(name="adaptdl", config=config)
