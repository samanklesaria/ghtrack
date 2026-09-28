#!/usr/bin/env python3
import os
import sys
from datetime import datetime, timedelta
from dateutil import parser as date_parser
import httpx
import trio

async def get_all(client, url, **params):
    """Paginate a GitHub list endpoint, retrying rate limits. Raises rather than dropping data."""
    items = []
    for page in range(1, 100):
        for attempt in range(6):
            response = await client.get(url, params={'per_page': 100, 'page': page, **params})
            if response.status_code == 200:
                break
            if response.status_code in (403, 429) or response.status_code >= 500:
                # ponytail: blind backoff; read retry-after/x-ratelimit-reset if this stays slow
                await trio.sleep(min(60, 2 ** attempt))
                continue
            raise RuntimeError(f"{url} page {page} returned {response.status_code}")
        else:
            raise RuntimeError(f"{url} page {page} still failing after retries")

        batch = response.json()
        items.extend(batch)
        if len(batch) < 100:
            break
    return items

def commit_activity_date(commit, username, days):
    """Date a commit landed under `username`, or None if it isn't theirs / is too old.

    Uses the committer as well as the author, and the later of the two dates: a rebase or
    force-push replays old authored dates but stamps a fresh committer date.
    """
    logins = {(commit.get(role) or {}).get('login') for role in ('author', 'committer')}
    if username not in logins:
        return None

    date = max(date_parser.isoparse(commit['commit'][role]['date']) for role in ('author', 'committer'))
    return date if date >= datetime.now(date.tzinfo) - timedelta(days=days) else None

def mine_and_recent(entries, username, days):
    """Keep the user's own comments/reviews from the last `days`, summarised for the report."""
    kept = []
    for entry in entries:
        if (entry.get('user') or {}).get('login') != username:
            continue

        # Reviews carry submitted_at, and are absent until submitted; comments carry created_at
        stamp = entry.get('created_at') or entry.get('submitted_at')
        if not stamp or entry.get('state') == 'PENDING':
            continue

        date = date_parser.isoparse(stamp)
        if date < datetime.now(date.tzinfo) - timedelta(days=days):
            continue

        # An approval or plain review request has no body, so fall back to its state
        body = entry.get('body') or entry.get('state', '').lower().replace('_', ' ')
        kept.append({'date': date, 'body': body[:100] + ('...' if len(body) > 100 else '')})
    return kept

async def fetch_commits_for_pr(client, repo, pr_number, pr, username, days):
    """Fetch commits for a single PR asynchronously."""
    commits_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/commits"

    try:
        user_commits = []

        for commit in await get_all(client, commits_url):
            commit_date = commit_activity_date(commit, username, days)
            if commit_date:
                user_commits.append({
                    'sha': commit['sha'][:9],
                    'date': commit_date,
                    'message': commit['commit']['message'].split('\n')[0]
                })

        if user_commits:
            pr_key = f"{repo}#{pr_number}"
            # Check if PR was merged (pull_request object has merged_at field)
            state = pr['state']
            if state == 'closed' and pr.get('pull_request', {}).get('merged_at'):
                state = 'merged'

            return {
                'key': pr_key,
                'data': {
                    'repo': repo,
                    'number': pr_number,
                    'title': pr['title'],
                    'url': pr['html_url'],
                    'state': state,
                    'is_author': True,
                    'commits': user_commits,
                    'comments': []
                }
            }
    except Exception as e:
        print(f"Error fetching commits for {repo}#{pr_number}: {e}", file=sys.stderr)

    return None

async def fetch_comments_for_item(client, item, username, days, is_issue=False, fetch_review_comments=False):
    """Fetch comments for a single PR or issue asynchronously."""
    repo = item['repository_url'].replace('https://api.github.com/repos/', '')
    number = item['number']
    comments_url = item['comments_url']

    try:
        user_comments = mine_and_recent(await get_all(client, comments_url), username, days)

        # Inline code comments plus the reviews themselves (an approval carries no inline comment)
        user_review_comments = []
        if not is_issue and fetch_review_comments:
            base = f"https://api.github.com/repos/{repo}/pulls/{number}"
            user_review_comments = mine_and_recent(await get_all(client, f"{base}/comments"), username, days)
            user_review_comments += mine_and_recent(await get_all(client, f"{base}/reviews"), username, days)

        if user_comments or user_review_comments:
            key = f"{repo}#{number}"
            # Check if PR was merged (pull_request object has merged_at field)
            state = item['state']
            if not is_issue and state == 'closed' and item.get('pull_request', {}).get('merged_at'):
                state = 'merged'

            result = {
                'key': key,
                'data': {
                    'repo': repo,
                    'number': number,
                    'title': item['title'],
                    'url': item['html_url'],
                    'state': state,
                    'is_author': False,
                    'comments': user_comments,
                    'review_comments': user_review_comments
                }
            }
            if is_issue:
                result['data']['is_issue'] = True
            return result
    except Exception as e:
        print(f"Error fetching comments for {repo}#{number}: {e}", file=sys.stderr)

    return None

async def search_activity(client, nursery, query, handle, search_counter):
    """Page through a search query, dispatching each hit to `handle` on the shared nursery.

    The search API allows only 30 requests/minute and we run several queries at once, so a 403
    here means "slow down", not "no more results" -- backing off instead of breaking is the
    difference between a full report and a truncated one.
    """
    for page in range(1, 11):
        params = {'q': query, 'per_page': 100, 'sort': 'updated', 'order': 'desc', 'page': page}

        for attempt in range(6):
            response = await client.get('https://api.github.com/search/issues', params=params)
            search_counter['count'] += 1
            if response.status_code == 200:
                break
            if response.status_code in (403, 429) or response.status_code >= 500:
                await trio.sleep(min(60, 2 ** attempt))
                continue
            print(f"Warning: search '{query}' page {page} returned {response.status_code}", file=sys.stderr)
            return
        else:
            print(f"Warning: search '{query}' page {page} rate-limited out; results are incomplete", file=sys.stderr)
            return

        items = response.json().get('items', [])
        for item in items:
            nursery.start_soon(handle, item)

        if len(items) < 100:
            return

def generate_report(pr_activity, comment_activity, username):
    # Collect all activity by date
    activity_by_date = {}
    url_to_info = {}

    for pr in pr_activity.values():
        url_to_info[pr['url']] = {
            'title': pr['title'],
            'state': pr['state'],
            'commits': len(pr['commits']),
            'comments': 0,
            'review_comments': 0
        }
        for commit in pr['commits']:
            date = commit['date'].strftime('%Y-%m-%d')
            if date not in activity_by_date:
                activity_by_date[date] = set()
            activity_by_date[date].add(pr['url'])

    for item in comment_activity.values():
        url = item['url']
        if url in url_to_info:
            # Update existing entry (PR with both commits and comments)
            url_to_info[url]['comments'] = len(item['comments'])
            url_to_info[url]['review_comments'] = len(item.get('review_comments', []))
        else:
            # New entry (only comments, no commits)
            url_to_info[url] = {
                'title': item['title'],
                'state': item['state'],
                'commits': 0,
                'comments': len(item['comments']),
                'review_comments': len(item.get('review_comments', []))
            }
        for comment in item['comments']:
            date = comment['date'].strftime('%Y-%m-%d')
            if date not in activity_by_date:
                activity_by_date[date] = set()
            activity_by_date[date].add(url)
        for review_comment in item.get('review_comments', []):
            date = review_comment['date'].strftime('%Y-%m-%d')
            if date not in activity_by_date:
                activity_by_date[date] = set()
            activity_by_date[date].add(url)

    if not activity_by_date:
        print("No activity found in the last 9 days.")
        return

    # Print by day
    for date in sorted(activity_by_date.keys(), reverse=True):
        day_name = datetime.strptime(date, '%Y-%m-%d').strftime('%A')
        print(f"# {day_name} ({date})")
        for url in sorted(activity_by_date[date]):
            info = url_to_info.get(url, {})
            title = info.get('title', "")
            state = info.get('state', 'unknown')
            state_label = f"[{state}]" if state in ['closed', 'merged'] else ""

            # Build activity summary
            activity_parts = []
            commits = info.get('commits', 0)
            comments = info.get('comments', 0)
            review_comments = info.get('review_comments', 0)

            if commits > 0:
                activity_parts.append(f"{commits} commit{'s' if commits != 1 else ''}")
            if comments > 0:
                activity_parts.append(f"{comments} comment{'s' if comments != 1 else ''}")
            if review_comments > 0:
                activity_parts.append(f"{review_comments} review comment{'s' if review_comments != 1 else ''}")

            activity_summary = f"({', '.join(activity_parts)})" if activity_parts else ""

            print(f"- {url} - {title} {state_label} {activity_summary}")
        print()

async def main():
    token = os.getenv('GITHUB_TOKEN')
    username = os.getenv('GITHUB_USERNAME')

    if not token:
        print("Error: GITHUB_TOKEN environment variable not set", file=sys.stderr)
        print("\nCreate a token at: https://github.com/settings/tokens", file=sys.stderr)
        print("Required scopes: 'repo' (or 'public_repo' for public repos only)", file=sys.stderr)
        sys.exit(1)

    if not username:
        print("Error: GITHUB_USERNAME environment variable not set", file=sys.stderr)
        sys.exit(1)

    # Validate credentials and check rate limit
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json'
    }
    try:
        async with httpx.AsyncClient(headers=headers) as client:
            auth_response = await client.get('https://api.github.com/user')
            if auth_response.status_code == 401:
                print("Error: Invalid GitHub credentials", file=sys.stderr)
                print("The provided GITHUB_TOKEN is not valid or has expired.", file=sys.stderr)
                print("\nCreate a new token at: https://github.com/settings/tokens", file=sys.stderr)
                print("Required scopes: 'repo' (or 'public_repo' for public repos only)", file=sys.stderr)
                sys.exit(1)
            elif auth_response.status_code != 200:
                print(f"Error: GitHub API returned status {auth_response.status_code}", file=sys.stderr)
                sys.exit(1)

            # Get and display rate limit information
            rate_limit_response = await client.get('https://api.github.com/rate_limit')
            if rate_limit_response.status_code == 200:
                rate_data = rate_limit_response.json()
                core_remaining = rate_data['resources']['core']['remaining']
                core_limit = rate_data['resources']['core']['limit']
                search_remaining = rate_data['resources']['search']['remaining']
                search_limit = rate_data['resources']['search']['limit']

                print(f"GitHub API Rate Limits:", file=sys.stderr)
                print(f"  Core API: {core_remaining}/{core_limit} requests remaining", file=sys.stderr)
                print(f"  Search API: {search_remaining}/{search_limit} requests remaining", file=sys.stderr)
                print(file=sys.stderr)

                # Abort if less than 15 search requests remaining
                if search_remaining < 15:
                    print(f"Error: Not enough search API requests remaining ({search_remaining} < 15)", file=sys.stderr)
                    print("Please wait for the rate limit to reset before running this script.", file=sys.stderr)
                    sys.exit(1)

    except httpx.HTTPError as e:
        print(f"Error validating credentials: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        # Fetch PR activity and comment activity in parallel
        search_counter = {'count': 0}
        since = (datetime.now() - timedelta(days=9)).isoformat()
        since_date = since[:10]

        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }

        async with httpx.AsyncClient(headers=headers, limits=httpx.Limits(max_connections=20)) as client:
            all_results = []

            async def keep(result):
                if result:
                    all_results.append(result)

            async def own_pr(pr):
                repo = pr['repository_url'].replace('https://api.github.com/repos/', '')
                await keep(await fetch_commits_for_pr(client, repo, pr['number'], pr, username, 9))

            async def commented_pr(item):
                await keep(await fetch_comments_for_item(client, item, username, 9, False, True))

            async def commented_issue(item):
                await keep(await fetch_comments_for_item(client, item, username, 9, True))

            async with trio.open_nursery() as nursery:
                for query, handler in [
                    (f'is:pr author:{username} updated:>={since_date}', own_pr),
                    (f'is:pr commenter:{username} updated:>={since_date}', commented_pr),
                    (f'is:issue commenter:{username} updated:>={since_date}', commented_issue),
                    (f'is:pr reviewed-by:{username} updated:>={since_date} -author:{username}', commented_pr),
                ]:
                    nursery.start_soon(search_activity, client, nursery, query, handler, search_counter)

            # Separate results into pr_activity and comment_activity
            pr_result = {}
            comment_result = {}
            for result in all_results:
                data = result['data']
                if data.get('is_author'):
                    pr_result[result['key']] = data
                else:
                    comment_result[result['key']] = data

        print(f"Total search requests made: {search_counter['count']}", file=sys.stderr)
        print(file=sys.stderr)

        generate_report(pr_result, comment_result, username)
    except httpx.HTTPError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    trio.run(main)
