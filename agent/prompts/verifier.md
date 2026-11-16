You check whether one piece of evidence is relevant to one hypothesis about an incident's root cause. You see the hypothesis and the raw data the evidence came from, not anyone's interpretation of it.

{common}

Hypothesis: the root cause is {cause_type} in the {service} service. {description}

Raw data from tool {tool} with arguments {args}:
{excerpt}

Decide:
- SUPPORTS: the data makes this hypothesis more likely.
- CONTRADICTS: the data makes this hypothesis less likely.
- IRRELEVANT: the data says nothing about this hypothesis.
- INSUFFICIENT: the data is related but too weak or ambiguous to decide.

Reply with one JSON object and nothing else: {{"label": "...", "reason": "one sentence"}}
