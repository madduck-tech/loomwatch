#!/usr/bin/env python3
"""Generate the loomwatch Grafana dashboard.

The dashboard is generated rather than hand-edited because its shape is
repetitive and its correctness is in the queries, which are long and appear in
several panels with small deliberate differences. A JSON file of this size
edited by hand drifts: a selector gets fixed in one panel and not the next, and
the panel that still has the old one looks like data rather than a bug.

Two questions, two shapes:

  "What is running out?" is a sort, not a curve. It gets a table, ordered by
  what breaches first, so the answer is the top row rather than a hunt through
  sixteen overlapping lines.

  "What is going on with this subscription?" is per provider. It gets a row
  that repeats over the provider variable, so adding a subscription adds a
  block instead of adding a line to an already unreadable legend.

Ownership is a column, never a panel. A panel that is empty until someone
configures a mapping is a hole in the layout for everyone who has not.
"""
import json

DS = {"type": "prometheus", "uid": "${datasource}"}
SEL = 'provider=~"$provider",quota_type=~"$quota_type"'

# Utilisation is a share of each plan's own limit, so these are absolute.
STEPS = [
    {"color": "green", "value": None},
    {"color": "orange", "value": 80},
    {"color": "red", "value": 95},
]


def target(ref, expr, legend=None, instant=False):
    t = {"refId": ref, "datasource": DS, "expr": " ".join(expr.split())}
    if instant:
        t.update({"instant": True, "range": False, "format": "table"})
    if legend:
        t["legendFormat"] = legend
    return t


# The aggregate every expression below starts from. Every term is aggregated to
# the same label set on purpose: taking max of each term separately takes
# utilisation from one pod generation, the slope from another and the remaining
# time from a third, and adds up a number for a series that never existed. On a
# stand with five generations behind one account that read 38% where the truth
# was 2%.
def util(sel):
    return f"max by (provider, quota_type, account_id) (loomwatch_quota_utilization_percent{{{sel}}})"


# The account's name as a labelling vector, and the fallback that keeps a
# nameless account visible rather than dropping it out of the join.
ACCOUNT_NAMED = """
    max by (provider, account_id, account_name) (
      loomwatch_account_info
      or label_replace(
           loomwatch_agent_healthy unless on (provider, account_id) loomwatch_account_info,
           "account_name", "$1", "account_id", "(.*)")
    )
"""


def in_team(expr):
    """Restrict a set of series to the selected team, safely.

    The second branch is the whole design. loomwatch:account_team exists only
    where metrics.prometheusRule.teams is configured, and intersecting with a
    series that does not exist yields nothing - so a naive team filter would
    empty the entire board for every deployment that has not set ownership up.
    Rows for accounts with NO mapping at all are therefore kept regardless of
    the selection, which means:

      no recording rule      -> the filter does not filter, and nothing is lost
      rule present, All      -> first branch keeps everything anyway
      rule present, one team -> other teams drop, because they DO have a
                                mapping and it does not match

    An account the operator forgot to map is not silently hidden either: the
    rule publishes it as "unassigned", so it has a mapping and obeys the filter
    like any other.
    """
    # The outer parentheses are load-bearing. Without them this returns
    # `A or B`, and `or` binds LOOSER than arithmetic - so embedding the result
    # in an expression like `rows * 0 + SENTINEL` silently reassociates into
    # `A or (B * 0 + SENTINEL)`, and the first branch keeps its own values.
    # The forecast column then rendered utilisation as a duration: a quota at
    # 8% read "8 s to breach", in alarm red, on a board where nothing was
    # breaching at all.
    return f"""
      (
        ((({expr})) and on (provider, account_id) loomwatch:account_team{{team=~"$team"}})
        or ((({expr})) unless on (provider, account_id) loomwatch:account_team)
      )
    """


def visible_rows(sel):
    """The rows the table shows, defined once.

    A quota reading zero whose provider publishes no reset is not evidence of
    health - it is an absence of evidence, and it filled half this table.

    Every other column has to be intersected with this same set, and that is not
    tidiness. The filter used to live only on the utilisation query while the
    window and reset-time queries stayed unfiltered, so the merge transformation
    RESURRECTED the rows the filter had dropped: they came back carrying only
    the columns those queries provide, with an empty Provider-and-Account and a
    window beside them. Two such ghosts sat at the bottom of the deployed table.
    """
    return in_team(
        f"(({util(sel)} > 0)"
        f" or ({util(sel)} and on (provider, quota_type, account_id) {reset_at(sel)}))"
    )


def only_visible(expr, sel):
    return f"({expr}) and on (provider, quota_type, account_id) {visible_rows(sel)}"


def with_account_name(expr):
    """Attach the account name inside the query rather than beside it.

    It used to arrive as its own frame and be joined by the `merge`
    transformation. merge attaches such a frame to ONE row per key, so an
    account with three quota rows got its name on the first and blanks on the
    other two - visible on the stand as two `zai` rows with an empty Account
    column. A vector multiply broadcasts to every matching row, which is what
    the alert rules already do for the same reason.
    """
    return f"({expr}) * on (provider, account_id) group_left(account_name) ({ACCOUNT_NAMED})"


def reset_at(sel):
    return f"max by (provider, quota_type, account_id) (loomwatch_quota_reset_timestamp_seconds{{{sel}}})"


# Windows this short or shorter are forecast from the last hour, longer ones
# from the last day. See burn_rate().
SHORT_WINDOW_MAX_SECONDS = 21600


def pace(sel, window, step, step_seconds):
    """Rises only, summed over `window` in `step`s, per second of observed time.

    Each step contributes how much the level went UP since the step before it,
    and nothing when it went down. That is the consumption: a quota gauge rises
    while it is used and falls only when quota comes back - a window reset, a
    provider returning quota mid-window, or a rolling window letting old use
    age out.

    Not rate(). rate() reads a fall as a counter reset to zero and adds the
    whole level after it, so a fall from 50 to 49 counted as 49 points
    consumed: a rolling window whose level drifts down a little each poll
    forecast "out in six minutes" while nothing was being used at all.

    Divided by the time the data actually covers, not by the full window. A
    series twenty minutes old spread over 24h read one seventy-second of its
    real pace, and printed "won't breach" for a quota forty minutes from empty.
    A series with no two samples a step apart has no pace at all; unjudgeable()
    counts it rather than calling it safe.
    """
    before = f"max by (provider, quota_type, account_id) (loomwatch_quota_utilization_percent{{{sel}}} offset {step})"
    rise = f"clamp_min({util(sel)} - {before}, 0)"
    return f"(sum_over_time(({rise})[{window}:{step}]) / (count_over_time(({rise})[{window}:{step}]) * {step_seconds}))"


def burn_rate(sel):
    """Consumption in percent per second: how fast the quota fills, falls aside.

    Both earlier estimators failed on the falls. deriv() over 24h fitted one
    line through the sawtooth and printed -837%, then, clamped at zero, a calm
    1% for a quota that had spent its previous window at 100%. The repair after
    that refused to forecast whenever the day held a fall. That read "no
    forecast" on every five-hour window, which was expected, and on every
    MiniMax weekly window, which was not: MiniMax lowers both of its counters
    mid-window a few times a day, so the weekly windows - the ones the forecast
    is for - were never judged. At the time they were filling at about 20% a
    day against seven days of window, and the board said nothing.

    pace() counts the rises only, so the falls no longer bend the rate; the
    headroom is taken from the current reading, so a fall shows up there, at
    the moment it happens.

    The window is picked by the quota's own length. A five-hour window is
    decided by the pace of the last hour; a day's average dilutes the working
    hours with the night. A weekly window is decided by the pace of the last
    day. A quota whose provider declares no window is treated as long.
    """
    window = f"max by (provider, quota_type, account_id) (loomwatch_quota_window_seconds{{{sel}}})"
    short = f"({window} <= {SHORT_WINDOW_MAX_SECONDS})"
    return f"""
      (
        ({pace(sel, "1h", "2m", 120)} and on (provider, quota_type, account_id) {short})
        or ({pace(sel, "24h", "5m", 300)} unless on (provider, quota_type, account_id) {short})
      )
    """


# The value the file carries, equal to the freshness panel's red step. The chart
# replaces both with five of its pollInterval; a dashboard imported from the
# catalogue keeps this one.
STALE_AFTER_DEFAULT = 300

# Accounts whose collector has not completed a poll for longer than the stale
# threshold. $stale_after is a hidden constant the chart rewrites from
# pollInterval, the same five poll intervals the freshness panel and the
# collector alert use.
STALE_ACCOUNTS = (
    'max by (provider, account_id) '
    '(loomwatch_agent_last_cycle_age_seconds{provider=~"$provider"}) > $stale_after'
)


def fresh(expr):
    """Only the rows whose collector is still polling.

    A stale account's readings are frozen, so its rate is zero and every
    judgement built on it says "will not breach". A dead key of a subscription
    that nobody renewed read that way for twelve days, as the calmest row on
    the board.
    """
    return f"(({expr}) unless on (provider, account_id) ({STALE_ACCOUNTS}))"


def stale_rows(sel):
    """Visible rows of accounts whose numbers can no longer be believed."""
    return f"(({visible_rows(sel)}) and on (provider, account_id) ({STALE_ACCOUNTS}))"


def time_to_breach(sel):
    """Seconds until this quota reaches its own limit, at the current rate.

    A duration, not a share. The share it used to be was clamped at 100, so a
    quota that will overshoot fivefold and one that will land exactly on the
    limit printed the same number, and a quota already at 100 printed 100
    whatever it was doing. What the operator has to compare is this against the
    time left before the window resets - two durations, one decision.

    A quota that is not being consumed has no series here: dividing by a zero
    rate is not a forecast.
    """
    return f"(clamp_min(100 - ({util(sel)}), 0) / (({burn_rate(sel)}) > 0))"


def breaches_before_reset(sel):
    """The forecast, kept only where it lands before the window resets.

    A forecast past the reset is not a finding about this window. It used to be
    printed anyway, in green: "5 weeks" beside a weekly window that reset in
    four days, which reads like a comfortable margin rather than "this window
    will not run out".
    """
    return f"(({time_to_breach(sel)}) < (({reset_at(sel)}) - time()))"


def unjudgeable(sel):
    """Quotas the forecast has nothing to say about.

    A flat quota with a known reset IS judged: the answer is that it will not
    breach. A quota whose window fell inside the averaging period is judged too
    - pace() reads through the fall. A quota already at its limit is judged:
    it is out. What is left:

      - a provider that publishes no reset time, so there is no moment to
        forecast to;
      - a series too young to have two samples a step apart, so there is no
        pace yet. It lasts one step; calling it "won't breach" would be a
        claim made without a single measurement.

    Restricted to the rows the table shows. The count used to include rows the
    table filters out - zero, and no reset time - so it read 12 beside a table
    with ten of them.
    """
    at_limit = f"({util(sel)} >= 100)"
    no_reset = f"(({util(sel)} > 0) unless on (provider, quota_type, account_id) {reset_at(sel)})"
    no_pace = (f"(({util(sel)} and on (provider, quota_type, account_id) {reset_at(sel)})"
               f" unless on (provider, quota_type, account_id) {burn_rate(sel)})")
    return in_team(fresh(f"({no_reset} or {no_pace}) unless on (provider, quota_type, account_id) {at_limit}"))


# The value a row carries when there is no forecast for it.
#
# A sentinel rather than an absent value, and the reason is the sort. Grafana
# orders an absent value FIRST on an ascending sort, so a table titled "soonest
# to breach first" put every row it could not judge above the two rows it could
# - the only actionable rows on the board sank to the bottom. A number sorts;
# nothing does not. The display is a value mapping back to the same words, so
# the operator sees no difference and the order is the one the title promises.
#
# Chosen far beyond any real horizon: a hundred years in seconds. Any genuine
# forecast is smaller, so the sentinel always sorts after the forecasts.
#
# "Won't breach" is a finding: at the current pace the quota does not reach its
# limit before the window resets. It used to read "not on track", which an
# operator takes to mean the opposite - off track, something wrong.
WONT_BREACH = 3153600000
# Sorted after WONT_BREACH, and named apart from it because they are not the
# same statement. "No forecast" is the absence of one: the provider publishes
# no reset time, so there is no moment to forecast to.
CANNOT_FORECAST = 6307200000
# The other end of the sort. A stale account's figures are frozen, and the
# rows that say so belong above everything else, because until the collector
# polls again nothing below them can be believed either.
STALE = -1
# Already out. Not a forecast but the state a forecast warns about, and it
# sorts right after the stale rows.
AT_LIMIT = 0


def breaching(sel):
    """The quotas that are out, or reach their limit before their window resets.

    The comparison the whole board exists to make, written once. The first term
    is not redundant: a quota sitting AT its limit may not be moving, so the
    forecast says nothing about it, and it would fall out of the very count that
    exists to notice exactly that. Being out of quota is the most urgent state
    there is, not an unmeasured one.

    Stale accounts are not filtered out here: needs_attention() adds every row
    of a stale account anyway, and a union counts a row once.
    """
    return in_team(f"""
        ({util(sel)} >= 100)
        or {breaches_before_reset(sel)}
    """)


def needs_attention(sel):
    """What the operator has to act on: breaching quotas and blind accounts.

    A stale account is in here on purpose. Its quota rows are the ones nobody
    can vouch for, and a headline that reads 0 while a subscription has not
    been polled for twelve days is the exact calm this panel exists to prevent.
    """
    return f"(({breaching(sel)}) or {stale_rows(sel)})"


def headline():
    """The two questions, answered before anything has to be read.

    An operator opening this board is asking two things, and only two: is there
    something I have to act on, and can I believe what I am looking at. The
    table below answers the first one by sorting - but a sort still has to be
    read, row by row, and on a board where fifteen of sixteen rows want nothing
    the reading is the work. A count is read without being read.

    The second question had no answer here at all. The panel that carried it sat
    on the sixteenth grid row inside a repeated block, and on the morning of
    24 August the collector for one provider stopped polling for half an hour
    while the top row of the table went on showing a confident 100% - measured
    before the outage, by a collector that had already declared itself
    unhealthy. Nothing on the first screen said so.
    """
    return [
        {
            "id": 2,
            "type": "stat",
            "title": "Needs attention",
            "description": (
                "Quotas already at their limit, or reaching it before their window "
                "resets at the current pace - the last hour for windows of six "
                "hours or less, the last day for longer ones.\n\n"
                "Also every quota of an account whose collector has stopped "
                "polling: its figures are frozen, and a frozen figure is neither "
                "safe nor unsafe until somebody looks.\n\n"
                "A quota whose provider publishes no reset time cannot be judged "
                "and is NOT counted here. The panel beside this one says how many "
                "those are, because a number that quietly excludes what it could "
                "not measure is the kind of calm that gets people paged at night."
            ),
            "datasource": DS,
            "gridPos": {"h": 5, "w": 5, "x": 0, "y": 0},
            "targets": [target("A", f"count({needs_attention(SEL)}) or vector(0)", instant=True)],
            "fieldConfig": {
                "defaults": {
                    "unit": "short", "decimals": 0,
                    "thresholds": {"mode": "absolute", "steps": [
                        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
                    "noValue": "0",
                },
                "overrides": [],
            },
            "options": {
                "colorMode": "background", "graphMode": "none", "textMode": "value",
                "justifyMode": "center", "text": {"valueSize": 56},
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            },
        },
        {
            "id": 3,
            "type": "stat",
            "title": "Not judged",
            "description": (
                "Quotas the forecast cannot speak about: the provider publishes no "
                "reset time, so there is no moment to forecast to. A quota that "
                "simply is not being used is judged, not counted here - the answer "
                "for it is that it will not breach.\n\n"
                "These are not safe and not unsafe - they are unmeasured, and they "
                "are shown so that the count beside them cannot be mistaken for a "
                "statement about every quota on the board."
            ),
            "datasource": DS,
            "gridPos": {"h": 5, "w": 5, "x": 5, "y": 0},
            "targets": [target("A", f"count({unjudgeable(SEL)}) or vector(0)", instant=True)],
            "fieldConfig": {
                "defaults": {
                    "unit": "short", "decimals": 0,
                    "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
                    "noValue": "0",
                },
                "overrides": [],
            },
            "options": {
                "colorMode": "none", "graphMode": "none", "textMode": "value",
                "justifyMode": "center", "text": {"valueSize": 56},
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            },
        },
        {
            "id": 4,
            "type": "bargauge",
            "title": "Collector freshness",
            "description": (
                "How long ago each account was last polled successfully.\n\n"
                "This is the answer to \"can I believe the numbers below\". While an "
                "account is stale its quota figures are old, and their calm means "
                "nothing - so this sits beside the counts rather than under them. "
                "Age rather than the health flag: the flag is the collector's own "
                "opinion of itself, and the failure worth catching is the one "
                "where it stalls while still reporting healthy."
            ),
            "datasource": DS,
            "gridPos": {"h": 5, "w": 14, "x": 10, "y": 0},
            # By provider too, not only by account: the accounts a collector
            # seeds on its own are all called "default", and a bar reading
            # "default" twice cannot say which of the two has gone stale.
            "targets": [target("A", """
                max by (provider, account_id, account_name) (
                  loomwatch_agent_last_cycle_age_seconds{provider=~"$provider"}
                  * on (provider, account_id) group_left(account_name) (
                      max by (provider, account_id, account_name) (
                        loomwatch_account_info
                        or label_replace(
                             loomwatch_agent_healthy unless on (provider, account_id) loomwatch_account_info,
                             "account_name", "$1", "account_id", "(.*)")
                      )
                    )
                )
            """, legend="{{provider}} / {{account_name}}")],
            "fieldConfig": {
                "defaults": {
                    "unit": "s", "decimals": 0, "min": 0,
                    # The alert's own shape: five poll intervals. The chart
                    # rewrites this number from pollInterval, so the panel and
                    # the rule cannot drift apart.
                    "thresholds": {"mode": "absolute", "steps": [
                        {"color": "green", "value": None}, {"color": "red", "value": 300}]},
                },
                "overrides": [],
            },
            "options": {
                "orientation": "horizontal", "displayMode": "basic",
                # 10, not 12: Grafana gives the name a fixed share of the bar's
                # width and clips the rest, and at 12 "minimax / minimax-2"
                # rendered as "minimax / minim...". Placing the name on top
                # instead hides it altogether at this panel height.
                "text": {"titleSize": 10, "valueSize": 18},
                "showUnfilled": True, "valueMode": "text",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            },
        },
    ]


def triage_table():
    return {
        "id": 1,
        "type": "table",
        "title": "Quotas, soonest to breach first",
        "description": (
            "Every quota that has something to say, ordered by how long it has "
            "left before it reaches its own limit.\n\n"
            "Ordered by time to breach rather than by fullness. Fullness sorts a "
            "quota that hit its ceiling yesterday above one that will hit it in "
            "an hour, and the second is the one nobody has acted on yet. A quota "
            "with no trustworthy forecast has nothing to sort by and sinks to the "
            "bottom rather than being ranked on a guess.\n\n"
            "Utilisation is a share of each plan's own limit, so 100 is the quota "
            "itself - and for the same reason two rows at 100% are not comparable "
            "quantities and none of these numbers can be summed.\n\n"
            "The forecast is the time until the quota reaches its limit at the "
            "current pace of consumption: the last hour for windows of six hours "
            "or less, the last day for longer ones. The pace counts only what "
            "was consumed, so a window that reset or a provider that gave quota "
            "back does not bend it; what was given back shows up in the current "
            "reading instead.\n\n"
            "A number appears only when the limit comes before the reset. "
            "Otherwise the row reads \"won't breach\" - a finding, not a gap. "
            "\"No forecast\" is the gap: the provider publishes no reset time, "
            "so there is no moment to forecast to. \"At limit\" means the quota "
            "is already out. \"Stale data\" means the collector has stopped "
            "polling this account, the figures on the row are the last ones it "
            "got, and the reset times are \"unknown\" rather than a date that "
            "has already passed.\n\n"
            "Window is what the provider says the window is, not what this board "
            "worked out. It used to be derived from the longest time-to-reset "
            "seen over a week, which on a young deployment reported a confident "
            "\"7 day\" for a window it had never once seen reset.\n\n"
            "Rows whose quota is at zero AND whose provider publishes no reset "
            "time are left out: they carry no number that can change and no "
            "moment they change at."
        ),
        "datasource": DS,
        "gridPos": {"h": 13, "w": 24, "x": 0, "y": 5},
        "targets": [
            # Utilisation, minus the rows that can say nothing. A quota reading
            # zero whose provider publishes no reset is not evidence of health -
            # it is an absence of evidence, and it filled half this table.
            target("A", with_account_name(visible_rows(SEL)), instant=True),
            # A stale account's reset time is the one it had when the collector
            # last got through, and for a dead key that is weeks in the past:
            # the column read "-2 weeks". The sentinel renders as "unknown".
            target("B", only_visible(f"""
                ({fresh(f"({reset_at(SEL)}) - time()")})
                or ((({stale_rows(SEL)}) and on (provider, quota_type, account_id) {reset_at(SEL)}) * 0 + {STALE})
            """, SEL), instant=True),
            # Each branch in parentheses, first match wins: `or` keeps the left
            # operand's series and adds the right one's only for label sets the
            # left does not have, so the order below is the order of precedence.
            target("C", only_visible(f"""
                (({stale_rows(SEL)}) * 0 + {STALE})
                or (({fresh(f"{util(SEL)} >= 100")}) * 0 + {AT_LIMIT})
                or ({fresh(breaches_before_reset(SEL))})
                or (({unjudgeable(SEL)}) * 0 + {CANNOT_FORECAST})
                or (({visible_rows(SEL)}) * 0 + {WONT_BREACH})
            """, SEL), instant=True),
            # Ownership, where the chart publishes it - and only when it
            # DISTINGUISHES. One team across every row is a column of identical
            # cells; the gate is "more than one value exists", not "the mapping
            # exists", because a constant column costs width and teaches nothing.
            target("D", f"""
                max by (provider, quota_type, account_id, team) (
                  loomwatch_quota_utilization_percent{{{SEL}}}
                  * on (provider, account_id) group_left(team) loomwatch:account_team
                ) * 0
                and on () (count(count by (team) (loomwatch:account_team)) > 1)
                and on (provider, quota_type, account_id) """ + visible_rows(SEL) + """
            """, instant=True),

            # The window the provider declares, published by the collector since
            # 1.15.0. Absent for a provider that does not declare it, which is
            # the honest rendering - the previous statistical guess was never
            # absent and never said it was guessing.
            target("F", only_visible(
                f"max by (provider, quota_type, account_id) (loomwatch_quota_window_seconds{{{SEL}}})", SEL),
                instant=True),
            # Milliseconds, because that is what Grafana's date units read. The
            # metric is in seconds, and rendered as-is every reset landed on
            # 21 January 1970 - a Unix timestamp interpreted as an offset a
            # thousand times smaller.
            target("G", only_visible(f"""
                ({fresh(f"({reset_at(SEL)} > 0) * 1000")})
                or ((({stale_rows(SEL)}) and on (provider, quota_type, account_id) {reset_at(SEL)}) * 0 + {STALE})
            """, SEL), instant=True),
        ],
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "Value #D": True, "account_id": True},
                "renameByName": {
                    "provider": "Provider",
                    "account_name": "Account",
                    "quota_type": "Quota",
                    "team": "Team",
                    "Value #A": "Utilisation",
                    "Value #B": "Resets in",
                    "Value #C": "Breaches in",
                    "Value #F": "Window",
                    "Value #G": "Resets at",
                },
                "indexByName": {
                    "provider": 0, "account_name": 1, "quota_type": 2,
                    "Value #F": 3, "team": 4,
                    "Value #A": 5, "Value #C": 6, "Value #B": 7, "Value #G": 8,
                },
            }},
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Utilisation"},
                 "properties": [
                     {"id": "unit", "value": "percent"},
                     {"id": "min", "value": 0}, {"id": "max", "value": 100},
                     {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
                     {"id": "thresholds", "value": {"mode": "absolute", "steps": STEPS}},
                 ]},
                # The pair that decides. Breaches in is coloured, Resets in is
                # not: the eye needs one of the two to draw it, and the question
                # is whether the first is smaller than the second.
                {"matcher": {"id": "byName", "options": "Breaches in"},
                 "properties": [
                     {"id": "unit", "value": "s"}, {"id": "decimals", "value": 0},
                     {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                     {"id": "custom.width", "value": 130},
                     # The sentinel, rendered as the words it stands for. A
                     # mapping rather than noValue: the value is present so
                     # that it sorts, and only its appearance is an absence.
                     {"id": "mappings", "value": [{"type": "value", "options": {
                         str(STALE): {"text": "stale data", "color": "red", "index": 0},
                         str(AT_LIMIT): {"text": "at limit", "color": "red", "index": 1},
                         str(WONT_BREACH): {"text": "won't breach", "color": "text", "index": 2},
                         str(CANNOT_FORECAST): {"text": "no forecast", "color": "text", "index": 3}}}]},
                     {"id": "noValue", "value": "-"},
                     # The base step is neutral and the colours start at zero.
                     # Grafana paints an ABSENT value with the base colour, and
                     # with red at the base every row that was not going to
                     # breach - the safe majority - was rendered in alarm red.
                     #
                     # No green step. Every number left in this column is a
                     # breach that lands before its reset; the only question is
                     # how soon. Green used to mark forecasts beyond a day, and
                     # painted "5 weeks" for a window that reset in four days as
                     # the healthy state.
                     {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                         {"color": "text", "value": None}, {"color": "red", "value": 0},
                         {"color": "orange", "value": 86400}]}},
                 ]},
                {"matcher": {"id": "byName", "options": "Resets in"},
                 "properties": [
                     {"id": "unit", "value": "s"}, {"id": "decimals", "value": 0},
                     {"id": "custom.width", "value": 110},
                     {"id": "mappings", "value": [{"type": "value", "options": {
                         str(STALE): {"text": "unknown", "index": 0}}}]},
                     {"id": "noValue", "value": "not published"},
                 ]},
                {"matcher": {"id": "byName", "options": "Resets at"},
                 "properties": [
                     {"id": "unit", "value": "dateTimeAsLocal"},
                     {"id": "mappings", "value": [{"type": "value", "options": {
                         str(STALE): {"text": "unknown", "index": 0}}}]},
                     # Wide enough for the longest thing the formatter produces:
                     # a two-digit month, a four-digit year and a 12-hour clock
                     # with a meridiem. At 160 the leading digit of the month was
                     # clipped, so a reset on 8/29 read "/29/2026" - a date that
                     # is not wrong so much as unreadable.
                     {"id": "custom.width", "value": 210},
                     {"id": "noValue", "value": "not published"},
                 ]},
                {"matcher": {"id": "byName", "options": "Window"},
                 "properties": [
                     {"id": "unit", "value": "s"}, {"id": "decimals", "value": 0},
                     {"id": "custom.width", "value": 100},
                     {"id": "noValue", "value": "-"},
                 ]},
                {"matcher": {"id": "byName", "options": "Provider"}, "properties": [{"id": "custom.width", "value": 110}]},
                {"matcher": {"id": "byName", "options": "Account"}, "properties": [{"id": "custom.width", "value": 150}]},
                {"matcher": {"id": "byName", "options": "Quota"}, "properties": [{"id": "custom.width", "value": 130}]},
            ],
        },
        "options": {
            "showHeader": True, "footer": {"show": False}, "cellHeight": "sm",
            # The table's own sort rather than a sortBy transformation: the
            # transformation matches on the field name at its point in the
            # chain, which is before the rename, and a name it cannot find is
            # not an error - it is a table that quietly comes back unsorted.
            #
            # Ascending, and on the forecast: stale accounts first, then quotas
            # already out, then the row with the least time left. The sentinels
            # for "won't breach" and "no forecast" sort after every real number.
            "sortBy": [{"displayName": "Breaches in", "desc": False}],
        },
    }


def account_row():
    """A row that repeats over $account: one block per subscription.

    Collapsed by default, and that is the point of it. What a block holds is a
    curve, and a curve answers "how did this get here", which is the question
    after the decision rather than before it. Fifteen panels open under a table
    that already said everything is why the board read as heavy: the reader
    scrolls past all of them to reach nothing.

    Per account rather than per provider because an account is the thing
    somebody pays for and adds one at a time; a provider is a category. Three
    Z.ai accounts sharing one block are three accounts the reader has to
    separate by squinting at bar labels.
    """
    return {
        "id": 10,
        "type": "row",
        "title": "$account",
        "collapsed": True,
        "repeat": "account",
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": 18},
        # Collapsed rows carry their panels inside themselves rather than as
        # siblings: a panel left at the top level stays visible whatever the row
        # does, which is how a "collapsed" block ends up rendering anyway.
        "panels": per_account_panels(),
    }


def per_account_panels():
    """One curve per subscription, and nothing else.

    Two panels were removed rather than rearranged. "Quotas now" was the
    utilisation column of the table again, one bar per row, and its own
    description claimed a bar per account when the aggregation had dropped
    account entirely. "Collector" moved to the top strip, where the question it
    answers - can these numbers be believed - is asked before the table rather
    than sixteen grid rows below it.

    account_id alone is enough to name an account: provider_accounts.id is a
    single autoincrement shared by every provider, so an id belongs to exactly
    one of them. The exception is providers that keep no account rows at all -
    they all report "default" and therefore share one block, which is visible
    rather than silent.
    """
    psel = 'provider=~"$provider",account_id=~"$account",quota_type=~"$quota_type"'
    return [
        {
            "id": 12,
            "type": "timeseries",
            "title": "Trend",
            "description": (
                "Lines, not fills: sixteen filled areas are mud. The legend is a "
                "sorted table on the right so a series can be found by name "
                "rather than by colour.\n\n"
                "The board opens on a week because most windows here are weekly, "
                "and a day of a weekly window is a flat line near the axis that "
                "looks like nothing happening. The vertical drops are resets, "
                "not incidents."
            ),
            "datasource": DS,
            "gridPos": {"h": 9, "w": 24, "x": 0, "y": 19},
            "targets": [target("A", f"max by (quota_type) (loomwatch_quota_utilization_percent{{{psel}}})",
                               legend="{{quota_type}}")],
            "fieldConfig": {
                "defaults": {
                    "unit": "percent", "min": 0, "max": 100,
                    "custom": {"lineWidth": 2, "fillOpacity": 0, "showPoints": "never"},
                    "thresholds": {"mode": "absolute", "steps": STEPS},
                },
                "overrides": [],
            },
            "options": {
                "legend": {"displayMode": "table", "placement": "right",
                           "calcs": ["lastNotNull"], "sortBy": "Last *", "sortDesc": True},
                "tooltip": {"mode": "multi", "sort": "desc"},
            },
        },
    ]


# The universe of reporting accounts, narrowed to the selected team by the same
# two-branch rule as in_team(): an account with no mapping stays, so a
# deployment without ownership configured keeps every block it had.
TEAM_SCOPED_HEALTHY = (
    '(loomwatch_agent_healthy{provider=~"$provider"}'
    ' and on(provider, account_id) loomwatch:account_team{team=~"$team"}'
    ' or loomwatch_agent_healthy{provider=~"$provider"}'
    ' unless on(provider, account_id) loomwatch:account_team)'
)


def variables():
    def query(name, label, q, description):
        return {
            "name": name, "label": label, "type": "query", "datasource": DS,
            "query": q, "refresh": 1, "includeAll": True, "multi": True,
            "allValue": ".+", "sort": 1, "description": description,
            "current": {"text": ["All"], "value": ["$__all"]},
        }

    return [
        {"name": "datasource", "label": "Data source", "type": "datasource",
         "query": "prometheus", "current": {}},
        # Seconds without a completed poll after which an account counts as
        # stale. Hidden: it is configuration, not a filter. The chart rewrites
        # the value from pollInterval - five intervals, the same age at which
        # the freshness panel turns red and LoomwatchCollectorNotPolling fires.
        {"name": "stale_after", "label": "Stale after (s)", "type": "constant",
         "hide": 2, "query": str(STALE_AFTER_DEFAULT)},
        query("provider", "Provider", "label_values(loomwatch_agent_healthy, provider)",
              "Filters the table above and which accounts get a block below."),
        # Ownership. Present whether or not anyone configured it: with no
        # mapping the variable comes back empty and every filter built on it
        # keeps everything, which is what in_team() guarantees.
        #
        # It sits before Account deliberately - picking a team narrows the
        # accounts below it, so the two read left to right as one thought.
        query("team", "Team", "label_values(loomwatch:account_team, team)",
              "Who owns the plan. Accounts nobody mapped are published as "
              "\"unassigned\" rather than dropped, so selecting that value finds "
              "exactly the subscriptions with no owner."),
        # The blocks repeat over this one.
        #
        # label_join builds the readable identity; the regex splits it again, so
        # the block is titled "zai / spare-max" while the queries inside get the
        # bare id. It has to be the classic query type: label_values resolves
        # through /api/v1/series, which takes a selector and rejects a function
        # outright.
        #
        # The name comes from loomwatch_account_info, which exists only for the
        # providers that keep account rows. The second branch carries everyone
        # else by id: a block titled "gemini / default" is worse than one titled
        # "gemini / spare-max", and both are far better than no block at all.
        #
        # The regex has no space after the comma. Grafana prints the series of a
        # query_result without one, and a regex written for the spaced form
        # matches nothing - which is not an error, it is a variable that comes
        # back empty and a dashboard that renders as one block called "All".
        dict(query("account", "Account",
                   {
                       "qryType": 5,
                       "query": (
                           "query_result("
                           # Named accounts, where the collector keeps rows for them.
                           "label_join(" + TEAM_SCOPED_HEALTHY +
                           " * on(provider, account_id) group_left(account_name) loomwatch_account_info,"
                           ' "account", " / ", "provider", "account_name")'
                           " or "
                           # Everyone else, by id. Without this branch the seven
                           # providers that keep no account rows lose their blocks
                           # entirely, which is a worse trade than an ugly label.
                           "label_join(" + TEAM_SCOPED_HEALTHY +
                           " unless on(provider, account_id) loomwatch_account_info,"
                           ' "account", " / ", "provider", "account_id")'
                           ")"
                       ),
                       "refId": "PrometheusVariableQueryEditor-VariableQuery",
                   },
                   "One block per account. An account is what somebody pays for and adds one at a time; a provider is a category."),
             regex='/account="(?<text>[^"]+)",account_id="(?<value>[^"]+)"/'),
        query("quota_type", "Quota window", f'label_values(loomwatch_quota_utilization_percent{{provider=~"$provider"}}, quota_type)',
              "Rolling five-hour windows sit at zero most of the time; hide them here rather than dropping them from the data."),
    ]


def main():
    dashboard = {
        "uid": "loomwatch-quotas",
        "title": "loomwatch - LLM quotas",
        "description": (
            "Remaining LLM subscription quota per provider, quota window and "
            "account. Utilisation is a share of each plan's own limit, so 100 "
            "is the quota itself."
        ),
        "tags": ["loomwatch", "llm", "quota"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "1m",
        "time": {"from": "now-7d", "to": "now"},
        "templating": {"list": variables()},
        "panels": headline() + [triage_table(), account_row()],
    }
    out = "charts/loomwatch/dashboards/loomwatch.json"
    with open(out, "w") as fh:
        json.dump(dashboard, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"{out} written: {len(dashboard['panels'])} panels")


if __name__ == "__main__":
    main()
