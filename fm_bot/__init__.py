"""FM24 club management bot.

The bot consumes the read-only observation bridge through its loopback JSON
API, keeps its own SQLite state, plans through explicit rules and bounded
optimisation, and operates the game only through a separate UI adapter.

Package layout follows the design specification, section 16.1:

* ``bridge_client``  transport, schema adapters, capability checks
* ``state``          identities, snapshots, units, visibility, journal
* ``rules``          eligibility, competitions, deadlines, authority
* ``planning``       squad, minutes, finances, recruitment, strategy
* ``models``         forecasts, calibration, dynamics, registry
* ``execution``      screen models, actions, verification, reconciliation
* ``interactions``   inbox parsing, choice mapping, promises
* ``experiments``    checkpoints, treatments, manifests, evaluation
* ``interface``      operator status, explanations, controls
* ``tests``          contract, fault, solver, leakage, live workflows

Nothing here writes to FM's memory. The bot runs in its own interpreter and
talks to the bridge over HTTP so that solver or model dependencies cannot
destabilise the decoder.
"""

__version__ = "0.1.0"
SPEC_VERSION = "1.0"
SUPPORTED_BUILD = "24.4.2+2081827"
BRIDGE_DEFAULT_URL = "http://127.0.0.1:8765"
