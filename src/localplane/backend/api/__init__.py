"""The HTTP surface.

These schemas are a *projection* of the domain model, not the domain model itself. The
internal representation is objects, observations and evidence; what a client receives is
assembled from those at read time. Keeping the two apart is what stops a convenience
shape a screen happened to want from becoming the thing the backend believes.
"""
