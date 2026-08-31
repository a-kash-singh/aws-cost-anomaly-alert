"""
AWS Cost Anomaly Slack Alert
=============================
Runs on a schedule (see template.yaml), pulls the last N days of AWS Cost
Explorer spend, compares the two most recent days, and posts a Slack alert
when total spend -- or any individual service -- jumps beyond a configurable
threshold.

Environment variables
----------------------
SLACK_WEBHOOK_URL      required   Incoming webhook URL to post alerts to.
MIN_PCT_INCREASE       optional   Minimum percent increase for a service to
                                  be flagged. Default: 10.
MIN_ABS_INCREASE_USD   optional   Minimum absolute USD increase for a
                                  service to be flagged. Default: 10.
LOOKBACK_DAYS          optional   Days of cost history to pull each run.
                                  Default: 7.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import boto3
import requests

EXCLUDED_SERVICES = ["Savings Plans for Compute usage", "Tax"]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def get_service_costs(lookback_days: int) -> dict:
    """Return {date_str: {service_name: cost}} for the last `lookback_days` days.

    Cost Explorer is a global AWS service, so this works no matter which
    region the Lambda itself is deployed in.
    """
    client = boto3.client("ce")

    end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=lookback_days - 1)

    response = client.get_cost_and_usage(
        TimePeriod={
            "Start": start_date.strftime("%Y-%m-%d"),
            "End": end_date.strftime("%Y-%m-%d"),
        },
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        Filter={
            "Not": {
                "Dimensions": {
                    "Key": "SERVICE",
                    "Values": EXCLUDED_SERVICES,
                }
            }
        },
    )

    service_costs = {}
    for result in response["ResultsByTime"]:
        date = result["TimePeriod"]["Start"]
        service_costs[date] = {
            group["Keys"][0]: float(group["Metrics"]["UnblendedCost"]["Amount"])
            for group in result["Groups"]
        }
    return service_costs


def find_service_increases(prev_costs: dict, latest_costs: dict, min_pct: float, min_abs: float) -> list:
    """Return (service, prev_cost, latest_cost, diff) tuples that cross both thresholds."""
    increases = []
    for service, latest_cost in latest_costs.items():
        prev_cost = prev_costs.get(service, 0)
        diff = latest_cost - prev_cost
        threshold = prev_cost * (1 + min_pct / 100)
        if latest_cost > threshold and diff >= min_abs:
            increases.append((service, prev_cost, latest_cost, diff))
    increases.sort(key=lambda row: row[3], reverse=True)
    return increases


def build_slack_message(prev_date, latest_date, total_prev, total_latest, increases, top_n=5) -> dict:
    total_diff = total_latest - total_prev
    total_pct = (total_diff / total_prev * 100) if total_prev > 0 else 0
    emoji = "\U0001F7E2" if total_diff <= 0 else "\U0001F534"
    change_type = "Decrease" if total_diff <= 0 else "Increase"

    services_text = "\n".join(
        f"{i + 1}. {service}\n"
        f"   ${latest:,.2f} vs ${prev:,.2f} "
        f"(+${diff:,.2f}, {f'{diff / prev * 100:.2f}%' if prev > 0 else 'new'})"
        for i, (service, prev, latest, diff) in enumerate(increases[:top_n])
    )

    text = (
        f"{emoji} *AWS Cost Alert:* `{total_pct:.2f}%` *{change_type} Detected*\n"
        f"*Cost {change_type.lower()} detected on* `{latest_date}`\n\n"
        f"{emoji} *Cost Summary:*\n"
        f"• *Current total cost:* `${total_latest:,.2f}`\n"
        f"• *Total cost on {prev_date}:* `${total_prev:,.2f}`\n"
        f"• *{change_type}:* ${total_diff:,.2f} `({total_pct:.2f}%)`\n\n"
        "\U0001F50D *Top Contributing Services:*\n"
        f"{services_text}\n\n"
        "\U0001F4A1 *Recommendations:*\n"
        "• Check for unexpected resource usage\n"
        "• Review recent deployments\n"
        "• Consider cost optimization strategies for the services above"
    )
    return {"text": text}


def send_slack_alert(service_costs: dict) -> None:
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("SLACK_WEBHOOK_URL is not set; skipping alert")
        return

    dates = sorted(service_costs.keys())
    if len(dates) < 2:
        print("Not enough data yet for a day-over-day comparison")
        return

    prev_date, latest_date = dates[-2], dates[-1]
    prev_costs, latest_costs = service_costs[prev_date], service_costs[latest_date]

    total_prev = sum(prev_costs.values())
    total_latest = sum(latest_costs.values())

    min_pct = _env_float("MIN_PCT_INCREASE", 10)
    min_abs = _env_float("MIN_ABS_INCREASE_USD", 10)
    increases = find_service_increases(prev_costs, latest_costs, min_pct, min_abs)

    if not increases:
        print(f"No service crossed the alert threshold on {latest_date}")
        return

    message = build_slack_message(prev_date, latest_date, total_prev, total_latest, increases)

    response = requests.post(
        webhook_url,
        data=json.dumps(message),
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if response.status_code == 200:
        print(f"Slack alert sent for {latest_date}")
    else:
        print(f"Slack alert failed ({response.status_code}): {response.text}")


def lambda_handler(event, context):
    lookback_days = _env_int("LOOKBACK_DAYS", 7)
    service_costs = get_service_costs(lookback_days)
    print("Fetched AWS cost data:", json.dumps(service_costs, indent=2))
    if service_costs:
        send_slack_alert(service_costs)
    return {"statusCode": 200, "body": "Execution completed"}
