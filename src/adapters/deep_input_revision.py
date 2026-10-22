# SPDX-License-Identifier: MIT-0
"""The declared revision of the deep-input code, carried in every deep cache key.

ADR-076 decision 1 (2026-10-05 amendment, design R5-5): deep enrichment runs only on a cache
miss, so the enriched input cannot be digested into the key it is looked up by. Each entry
pairs a revision name with the digest of the deep-input code's in-repository import closure
plus the metric dictionary (``tests/unit/test_deep_input_revision.py`` computes it). The test
fails until a new entry is appended after any change there, so a deep-input change always
moves every deep key. Append only: never edit or remove an entry. Revisions that never
deployed are harmless.
"""

from __future__ import annotations

from typing import Final

DEEP_INPUT_REVISIONS: Final[tuple[tuple[str, str], ...]] = (
    ("2026-10-05.1", "2a30c912d6742dc6a7180be244f44c9f64a1bca94671f9d3875e3d7df5edf466"),
    ("2026-10-05.2", "0c211fd74d9d5ac168ad2ceab69971b5e1af4c7087ead6db45245265a89a29f3"),
    ("2026-10-07.1", "4f228438d39f85a968656fab952ba1baa3f588fc6f5495721104c05e5c36bd78"),
    ("2026-10-07.2", "3a0fca7539e938911169bb5ef9b798cf1f995f4c8e2aa630c12f6bff3b6fb503"),
    ("2026-10-07.3", "acec9afbe8f53a7aa9067bdc3facaf9dfde8ddb0a75a16c76d5e355687ae9f2b"),
    ("2026-10-07.4", "9f90a94c71dbde64d0ad0678846a12f229498ab102240f0b54f20afa35d08fe0"),
    ("2026-10-07.5", "5f8766db4e2366be090198c820e2bbb7bd041c6c3c29a3983a93f62d042873e6"),
    ("2026-10-07.6", "2475738fb7a6605604374b7989516711d9a4b4350cb3110298bb4025f8821ed0"),
    ("2026-10-07.7", "1a6dfd7efec1fda377c684907bb589affc09fde03d50de435f5e3af5fab4c496"),
    ("2026-10-07.8", "be662222be277d4bf1cc44a437a002373b1d4ec5c15a52fd5fa35064e8f7c298"),
    ("2026-10-07.9", "ba9232c729c8701a351d25fecb49e7a4c63b48f83f9d4cd61a210c96c948c409"),
    ("2026-10-08.1", "824cec4a8c0f1d0d177d72627086cbfd640e786eece8c90fcfaa9c6f3caf8b8d"),
    ("2026-10-08.2", "430ba5f71ae601da567faca6020fb88a9f0418c0835a11d8e8a73ffd5162acc9"),
    ("2026-10-08.3", "a16ed252a797541b1157a8c44e7ca87e82e0a8305302e2bd2441e91411d57ed5"),
    ("2026-10-08.4", "117efec00d0b13b44561051f8888f43aa879298449144b2d6265303da79b096a"),
    ("2026-10-08.5", "ea7bd86a742934b07b6ee5b8de2a15d81f403a8385ae32ae3a19b6b80fa4eb9b"),
    ("2026-10-08.6", "ef0f042b41afb953c9ff3685afb900f9596ead9c6a5afcba2a6edd32243a08cd"),
    ("2026-10-08.7", "ffd8d7987b389e1aa93a05d6842f66a1b3922d566bae5eb9b2c9a91a76be9977"),
    ("2026-10-08.8", "69655bb97c169c37625932f8066cd94c4bb6d2fd0a4782486ce295a49e965015"),
    # 2026-10-09.1 to .8 are feat/bo-wave2's 2026-10-08.1 to .7 and 2026-10-09.1, renumbered
    # past feat/ui-ia-impl at the integration merge; .9 is the merged closure.
    ("2026-10-09.1", "c6b3bc6f5afd3bf9486e1e73ae7501885041a35f999037c96b7bad932fbfe427"),
    ("2026-10-09.2", "b51cab6ae5414f10e5a336aa3d0e3c3b259c40fba84fba88795a1f62531d9383"),
    ("2026-10-09.3", "3d8452f5fec8a33996447969f5b59e7703383d6646966b93e1e7434fd6395bf7"),
    ("2026-10-09.4", "79ad29cedbc411c2946fd6ec657e25c08717b39bde9f6a7ebcfddb3c9ca2089d"),
    ("2026-10-09.5", "203a49be3aa92fa605c345f802f2b8bd405392a9c32a44ae5c20d75d10fdc691"),
    ("2026-10-09.6", "80919b86d34b22266d7be5913a34b7cb24ea97a67acabf1312db1eb05b32cc4a"),
    ("2026-10-09.7", "5559d401af0ead760e7c219da0e39f922a4470ae5baabb547f78195860cca49a"),
    ("2026-10-09.8", "342b3193ddc8ab4ea61ee8553f6c450e565f6778cad93093f5ffbb69e32c914f"),
    ("2026-10-09.9", "fa644c9e1b727a4d2c1beef152547f6055a236d806da4c79d10aee9621d2051f"),
    ("2026-10-09.9.1", "f3d7940ba9ef8fc62b8e906695798a9ba36fb2ab08bb2d67cfe6e404ccffd337"),
    ("2026-10-09.9.2", "b657da6b5728fd013693567672c698b313c4272232cea6c765506807156afccb"),
    ("2026-10-09.9.3", "e2321d50236a416f5b6431b035b420e14c99b1af8b6b6f09b249393cd24bc174"),
    # The feat/insight-data-ux-refresh S6 part 2 merge closure. That branch's own 2026-10-09.1
    # to .7 (graph seam re-exports and error codes, slices 3 to 5ii) never deployed and collided
    # with the names above, so they are folded into this one entry.
    ("2026-10-09.9.4", "6cf3768c0b78862fddbc3874f10aa1e472aef657b6f1e0e35cedacdb54584e88"),
    # S6 part 2 slice 7 (1b): the GRAPH_UNIT error codes in domain.shared_kernel.errors.
    ("2026-10-10.1", "671efe944275ed30c7b99438261432960dd4f3fe162dc48df4a6b8df83a17c8f"),
    # Slice 7 (1b, D37): the batch span constant in the generated spans and tracing re-export.
    ("2026-10-10.2", "fef32ff68f3950502a32a42e395c7b757027c2fbc08f0c8a8b90dfbfbf6bbe0c"),
    # The feat/hardening-no-silent-failure merge closure. That branch's own 2026-10-09.9.4,
    # 2026-10-09.9.5 and 2026-10-11.1 (reported closure errors and the ruff format) never
    # deployed and its 2026-10-09.9.4 collided with the name above, so they fold into this one.
    ("2026-10-11.2", "5206eeca8faf3fa29742b7407cde6d5310bb12daf9218646a904cd58b50f52e5"),
    # The S6 part 2 slice 5s merge closure. feat/insight-data-ux-refresh's own 2026-10-11.1 (the
    # D7 placeholder error codes retired in domain.shared_kernel.errors) never deployed and
    # collided with the hardening name folded above, so it folds into this one.
    ("2026-10-11.3", "bc95727fdf2df19e5e1faf954738c648befffa15148aa59c9ea9d5e48b5a1c84"),
    # feat/graph-bake-delivery: the V3 bake spans in the generated spans and tracing re-export,
    # the per-window preset-artifact plan counts and sibling pairs, and the precompute insight
    # handler's model-concurrency telemetry.
    ("2026-10-13.1", "c310ce79caa3fb4cedfa4548f8327365559719e86c361783da402c3a5e388de5"),
    # feat/graph-bake-delivery FEAT-003 and its closure: the cohort-correlation span context,
    # the conditional athena.query correlation, and the promoter pass/domain span constants.
    ("2026-10-15.1", "b7c4c69029a6d2dd765860d32f0ff59b96d88bfe25d66763e48252d928b17382"),
    # The D2 integration merge closure (feat/ui-ia-impl 041e0235 and feat/reach-presentation
    # e36cd38f into feat/graph-bake-delivery). That branch's own 2026-10-10.1 to 2026-10-13.9.2
    # never deployed and collided with the names above, so they fold into this one entry.
    ("2026-10-15.2", "d54ae0429e0056b2e884f0ee98d5220563523259823a5ef2312369e6e865ea6a"),
    # D2 spike S0's horizon-bounded whole-scope reads (plan A5.1 I9): the entity catalog and
    # segment labels read the newest publication only, and the CURRENT port takes a horizon.
    ("2026-10-15.3", "b5ecfb125628bf1f549790c7dee3e0ea1fa3735a83ef42f29ea887a24db2eeb2"),
    # ADR-139: the page absence catalog, the grant-serving refusal count and the one serving
    # predicate enter the handler's closure.
    ("2026-10-16.1", "e942bfb0fe109530603360d93c7888f88df8006134bea31b9d9712e1436c00ce"),
    # D2 chat seams: typed filter binds and no fold over an unstored suppression flag
    # (379098fe, deployed without its entry), a no-period request on a periodic metric at the
    # latest measured period, and the governed "last quarter" reference period.
    ("2026-10-17.1", "8b99866553399a2a29efce8912783f1e4b605b22de9c39d0dce8c5650dfbad55"),
    # D2 chat answers: an exact `requires` selector pinned as a governed default, the response's
    # non-additivity marker with no total across an exact-only axis, and the NTB synonyms.
    ("2026-10-17.2", "ea0520e6d022abc278228eadb51817079842c6a4d8c2443de057ba0bac1b7911"),
    # Review r1 of 2026-10-17.2: the marker on compared rows, grain-name wording, and no pin on a
    # `requires` key that is also one of the metric's grains.
    ("2026-10-17.3", "c786df1253098343a4031a622522cf81c3bfe57e57b0c25c8be6783f4d792e6f"),
    # Review r2 of 2026-10-17.3: the comparison answer's additivity inputs are required.
    ("2026-10-17.4", "3ee4bbbf88fbce1421eeadb9731ebd412588438707f4d73036b2fba3ae200655"),
    # The D2 phase 2 merge closure: fix/graph-noeligible-settle (with fix/serving-resilience,
    # whose 2026-10-16.1 sits above in name order) into feat/d2-integration.
    ("2026-10-17.5", "dc56ea7562d74a67f1c9f2fd64de069398003cc06a89149738d5beb8db8ac524"),
    # campaign-detail-500: portfolio deep inputs read only the focus metrics a sibling declares,
    # and a campaign's delivery requests DSP media quality only for an Amazon DSP selection.
    ("2026-10-18.1", "dcd53f68a5c4f487bc0dabe2143282ce3667177612b003be9f09276981e10ee5"),
    # campaign-detail-500 review 3: the media-pin refusal's context keys join the declared
    # error-context vocabulary in telemetry reporting.
    ("2026-10-18.2", "1b7e0f9421fc6138ee794b7de115e4598a761740f1e79e05d7dfbf27dac48f9c"),
    # dsp-cpc-derive: CPC derived from accepted spend and clicks, not a planned quotient.
    ("2026-10-19.1", "c3316551d07ffb2cb480e4b48a54e1319fc92860a275aced749af9c961dcc63c"),
    # ui-refresh 1.1: resolve_temporal gives a calendar default no day-count default window.
    ("2026-10-22.1", "371b804b7a9c3821226d37e353c53fc5b8c47edf02c486b5ac19821a17d8a4a1"),
)
DEEP_INPUT_REVISION: Final[str] = DEEP_INPUT_REVISIONS[-1][0]
"""The revision every deep cache key carries (``#DEEPINPUT#<revision>``)."""
