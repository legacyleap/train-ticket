"""Vouch — architectural conformance for pull requests (demo engine).

Two layers, mirroring the product definition:

* the *expensive* layer (`vouch.infer`) reads the repository once and produces the
  intended architecture: contexts, layers, rules, and the module import graph;
* the *cheap* layer (`vouch.review`) diffs a pull request and runs deterministic checks
  against that model, on every PR.

Stdlib only, so it runs on any checkout without installing that checkout's dependencies.
"""

__version__ = "0.1.0"
