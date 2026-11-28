"""A real Hermes agent turn against Olo's customer-context tool (acceptance).

``turn`` builds a disposable business, runs one ordinary Hermes ``AIAgent``
turn through a metered upstream (``metered``) and judges the final answer
against the commitment ledger.  Offline tests drive it with a scripted
loopback model; the live run needs its own approval.
"""
