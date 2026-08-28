# Code-feature extraction and assessment boundary

`CodeFeatureSet` is immutable extraction data. Its replay identity is defined
only by the sample, extractor/tool compatibility versions, and extraction
parameters; it is independent of mutable or investigation-pinned assessment
datasets.

Goodware and ReferenceCorpus assessments belong to P09/P10. Changing a pinned
goodware baseline or corpus therefore changes measurements and snapshot state,
but never reruns SMDA or changes the stored extraction. Legacy embedded
goodware/corpus assessment fields are ignored when code-feature payloads are
read and are not written by new extraction payloads.
