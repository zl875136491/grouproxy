# Pinned CN Routing Data

The monitor embeds these binary sing-box rule-sets and verifies their
SHA-256 values before writing them below its private state directory. No
node downloads routing data at runtime.

| Asset | Source commit | SHA-256 |
| --- | --- | --- |
| `geoip-cn.srs` | `SagerNet/sing-geoip` `b9c5e675b4d5359d4b47f4434fa7ae77e9991306` | `0acf5dad38fba9db2dade29ce5e4edc6902220944f30628ae46ed16cb0ec5edd` |
| `geosite-cn.srs` | `SagerNet/sing-geosite` `f0a27e83e462d93efa2632c999fd8ccfce971de8` | `63f6ef9ca510efd74cfa7def8e1e093a781886558d8aad4760984fddb16811ef` |

The source projects license the data under GPL-3.0-or-later; the matching
license text is included as `LICENSE`. Refreshing either asset requires an
explicit source revision, hash update in `routingdata.go`, `sing-box rule-set
decompile` validation, monitor tests, and a rebuilt monitor artifact.
