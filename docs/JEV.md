# Jev: decisions from a decision model

Jev (TypeSafe AI, https://docs.typesafe.ai) answers typed questions
about a state with probabilities instead of text: a **choice** among up
to 255 named options, with a probability per option and a confidence; a
**noul**, the probability that a condition holds; and a **score**, a
probability-weighted position on an ordered rubric of 2 to 10 levels.
Confidence measures how concentrated the probabilities are (for a
choice, `(p_max - 1/n) / (1 - 1/n)`). A request carries up to 32,000
tokens; input costs $42 per billion tokens and output nothing; an answer
takes about 0.3 s. TypeSafe publishes no calibration figures, so epicprod
measures them on its own record before acting on them.

## The client

`swf_epicprod/jev.py` is the one client: `decide(state, questions)`
posts to `https://api.typesafe.ai/v1/systemone` (model `jev-latest`) with
`TYPESAFE_API_KEY`, retries unavailability, overload and rate limiting
(503, 529, 429) with backoff, and returns the answers with the input
tokens and the estimated cost. The key is in the production-operations
agent's environment; the web tier holds none and reads Jev's results
only from stored products.

## Measurement on the request links

`scripts/jev_replay.py` replays PCS's request-to-configuration links
through a Jev choice among the 255 configurations sharing the most
weighted words with the request, and reports agreement by confidence
band against word overlap's own top pick. The run of 2026-10-07:

| Set | Answered | Jev agrees | Word overlap agrees | Confidence ≥ 0.9 |
|---|---|---|---|---|
| Requests anchored from their EVGEN path (exact links) | 397 | 78% | 53% | 320 answers, 90% agree |
| Request-form questionnaires (links made by earlier model matchers) | 54 | 57% | 56% | 5 answers |

Of the 86 disagreements on the exact links, nearly all fall between a
configuration and its near-duplicate: a plain configuration and its
beam-effects twin (pc370 and pc6553, `ip6_ep_275x18`), or a configuration
with its generator recorded and a twin recorded as `unrecorded` (pc279
and pc6463). A request's EVGEN path marks the beam-effects variant
(`_ABCONV`), which the configuration descriptions do not state. Seven per
cent of the calls failed with 503 before the client retried. A
questionnaire usually maps to several configurations (Q² bins), which
one choice cannot express; a noul per candidate configuration is the
form for it.

## Neighbours: configurations like this one

For each physics configuration, `swf_epicprod/config_neighbors.py`
ranks its nearest configurations in physics. Word overlap narrows the
catalog to 60 candidates; Jev scores every candidate in one request on
one rubric, ascending: unrelated physics; related physics (the same
broad process family, a different reaction); the same process at a
different beam energy or species; the same process and beam at a
different kinematic range or sample variant; the same physics, beam and
range with a different generator, generator version, radiative setting
or beam-effects variant; the same configuration. The order follows what
a requester looking for an existing sample wants first. Each
configuration is given as fields (its physics parameters, the generator
with version and radiative setting, the beam-effects variant): given one
line of text instead, Jev placed a different-beam configuration fourth
for pc434 and its own Q² siblings twenty-second and twenty-fifth; given
fields, the different beam falls to twenty-seventh and the siblings rise
to eighth to tenth.

The production-operations agent computes every configuration nightly
(`jev_config_neighbors`, about 1,000 requests, about 11 million input
tokens, about $0.50) and one configuration on request, into the cached
product `jev_config_neighbors`; a neighbour at the top level is listed as
a possible duplicate. The configuration page shows the fifteen nearest
in its card "Configurations like this one", marked experimental and
citing Jev; on the find page, every produced dataset's row names its
configuration with a link to the same card.

## Plain-language search

The find page's "Jev, which configurations?" takes the bar's words, a
request in plain language ("18 on 275 neutral-current DIS above Q² of 10,
radiative"), and returns the configurations that answer it, ranked
(`swf_epicprod/jev_like.py`). Word overlap narrows the catalog to 60
candidates; Jev scores each on one rubric, ascending: not what the
request asks for; related physics; the requested process at a different
beam; the requested process and beam at a different range or sample
variant; the request apart from the generator version, radiative setting
or beam-effects variant; exactly what the request asks for. A catalog
with no configuration for the request says so by ranking everything
below a match.

The click posts the words (`/pcs/find/jev/`); words ranked within the
week are answered from the store (`jev_like:<key>`, keyed by the
normalized words). Otherwise the production-operations agent ranks them
(doer `jev_like`, about 0.5 s and $0.0004), stores the answer and
publishes `jev_like_ready`; the page, which opened a short-lived stream
on the click with a bounded poll behind it, reads the stored answer
(`/pcs/find/jev/<key>/`). The web tier makes no call to Jev and holds no
key. A session may ask 60 times in ten minutes.
