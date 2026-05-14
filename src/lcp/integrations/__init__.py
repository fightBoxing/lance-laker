"""Outbound integrations with external services (Gravitino, model serving, ...).

Why a dedicated package: keep ``services/`` reserved for *control-plane
business logic* and put *transport-level* clients here, so the dependency
direction stays one-way (services -> integrations, never the reverse).
"""
