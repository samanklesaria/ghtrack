#!/usr/bin/env python3
import os
import sys
from datetime import datetime, timedelta
from dateutil import parser as date_parser
import httpx
import trio

async def fetch_commits_for_pr(client, repo, pr_number, pr, username, days):
    """Fetch commits for a single PR asynchronously."""
    commits_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/commits"

    try:
        # Fetch all pages of commits
        commits = []
        page = 1
        while True:
            params = {'per_page': 100, 'page': page}
            commits_response = await client.get(commits_url, params=params)

            if commits_response.status_code != 200:
                if page == 1:
                    return None
                break

            page_commits = commits_response.json()
            if not page_commits:
                break

            commits.extend(page_commits)
            page += 1

        user_commits = []

        for commit in commits:
            if commit.get('author') and commit['author'].get('login') == username:
                commit_date = date_parser.isoparse(commit['commit']['author']['date'])
                if commit_date >= datetime.now(commit_date.tzinfo) - timedelta(days=days):
                    user_commits.append({
                        'sha': commit['sha'][:7],
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

async def get_pr_activity(client, nursery, username, days, all_results, search_counter, since_date):
    """Fetch PR activity with pagination, stopping when pages are empty."""
    search_url = 'https://api.github.com/search/issues'
    query = f'is:pr author:{username} updated:>={since_date}'

    # Fetch pages sequentially, stop when empty
    for page in range(1, 11):  # Pages 1-10
        params = {'q': query, 'per_page': 100, 'sort': 'updated', 'order': 'desc', 'page': page}

        try:
            response = await client.get(search_url, params=params)
            search_counter['count'] += 1

            if response.status_code != 200:
                print(f"Warning: PR search page {page} returned {response.status_code}", file=sys.stderr)
                break

            data = response.json()
            prs = data.get('items', [])

            if not prs:
                break  # Stop pagination if page is empty

            # Fetch commits for all PRs on this page using the parent nursery
            async def fetch_and_store(pr):
                repo = pr['repository_url'].replace('https://api.github.com/repos/', '')
                pr_number = pr['number']
                result = await fetch_commits_for_pr(client, repo, pr_number, pr, username, days)
                if result:
                    all_results.append(result)

            for pr in prs:
                nursery.start_soon(fetch_and_store, pr)

        except Exception as e:
            print(f"Error fetching page {page}: {e}", file=sys.stderr)
            break

async def fetch_comments_for_item(client, item, username, days, is_issue=False, fetch_review_comments=False):
    """Fetch comments for a single PR or issue asynchronously."""
    repo = item['repository_url'].replace('https://api.github.com/repos/', '')
    number = item['number']
    comments_url = item['comments_url']

    try:
        # Fetch all pages of regular comments
        comments = []
        page = 1
        while True:
            params = {'per_page': 100, 'page': page}
            comments_response = await client.get(comments_url, params=params)

            if comments_response.status_code != 200:
                if page == 1:
                    return None
                break

            page_comments = comments_response.json()
            if not page_comments:
                break

            comments.extend(page_comments)
            page += 1

        user_comments = []

        for comment in comments:
            if comment['user']['login'] == username:
                comment_date = date_parser.isoparse(comment['created_at'])
                cutoff_date = datetime.now(comment_date.tzinfo) - timedelta(days=days)

                if comment_date >= cutoff_date:
                    user_comments.append({
                        'date': comment_date,
                        'body': comment['body'][:100] + ('...' if len(comment['body']) > 100 else '')
                    })

        # Fetch review comments (inline code comments) for PRs
        user_review_comments = []
        if not is_issue and fetch_review_comments:
            review_comments_url = f"https://api.github.com/repos/{repo}/pulls/{number}/comments"

            try:
                # Fetch all pages of review comments
                review_comments = []
                page = 1
                while True:
                    params = {'per_page': 100, 'page': page}
                    review_response = await client.get(review_comments_url, params=params)

                    if review_response.status_code != 200:
                        break

                    page_review_comments = review_response.json()
                    if not page_review_comments:
                        break

                    review_comments.extend(page_review_comments)
                    page += 1

                for comment in review_comments:
                    if comment['user']['login'] == username:
                        comment_date = date_parser.isoparse(comment['created_at'])
                        cutoff_date = datetime.now(comment_date.tzinfo) - timedelta(days=days)

                        if comment_date >= cutoff_date:
                            user_review_comments.append({
                                'date': comment_date,
                                'body': comment['body'][:100] + ('...' if len(comment['body']) > 100 else '')
                            })
            except Exception as e:
                print(f"Error fetching review comments for {repo}#{number}: {e}", file=sys.stderr)

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

async def fetch_pr_comments(client, nursery, username, since_date, days, all_results, search_counter):
    """Fetch PRs where user commented (pages sequentially, stop when empty)."""
    search_url = 'https://api.github.com/search/issues'
    query = f'is:pr commenter:{username} updated:>={since_date} -author:{username}'

    for page in range(1, 11):
        params = {'q': query, 'per_page': 100, 'sort': 'updated', 'order': 'desc', 'page': page}

        try:
            response = await client.get(search_url, params=params)
            search_counter['count'] += 1

            if response.status_code != 200:
                print(f"Warning: PR comment search page {page} returned {response.status_code}", file=sys.stderr)
                break

            data = response.json()
            items = data.get('items', [])

            if not items:
                break  # Stop pagination if page is empty

            # Fetch comments AND review comments for all items on this page using the parent nursery
            async def fetch_and_store(item):
                result = await fetch_comments_for_item(client, item, username, days, False, True)
                if result:
                    all_results.append(result)

            for item in items:
                nursery.start_soon(fetch_and_store, item)

        except Exception as e:
            print(f"Error fetching PR comment page {page}: {e}", file=sys.stderr)
            break

async def fetch_issue_comments(client, nursery, username, since_date, days, all_results, search_counter):
    """Fetch issues where user commented (pages sequentially, stop when empty)."""
    search_url = 'https://api.github.com/search/issues'
    query = f'is:issue commenter:{username} updated:>={since_date} -author:{username}'

    for page in range(1, 11):
        params = {'q': query, 'per_page': 100, 'sort': 'updated', 'order': 'desc', 'page': page}

        try:
            response = await client.get(search_url, params=params)
            search_counter['count'] += 1

            if response.status_code != 200:
                print(f"Warning: Issue comment search page {page} returned {response.status_code}", file=sys.stderr)
                break

            data = response.json()
            items = data.get('items', [])

            if not items:
                break  # Stop pagination if page is empty

            # Fetch comments for all items on this page using the parent nursery
            async def fetch_and_store(item):
                result = await fetch_comments_for_item(client, item, username, days, True)
                if result:
                    all_results.append(result)

            for item in items:
                nursery.start_soon(fetch_and_store, item)

        except Exception as e:
            print(f"Error fetching issue comment page {page}: {e}", file=sys.stderr)
            break

async def fetch_review_comments(client, nursery, username, since_date, days, all_results, search_counter):
    """Fetch PRs where user made reviews or review comments (pages sequentially, stop when empty)."""
    search_url = 'https://api.github.com/search/issues'
    # Use reviewed-by OR commenter to catch all review activity
    query = f'is:pr reviewed-by:{username} updated:>={since_date} -author:{username}'

    for page in range(1, 11):
        params = {'q': query, 'per_page': 100, 'sort': 'updated', 'order': 'desc', 'page': page}

        try:
            response = await client.get(search_url, params=params)
            search_counter['count'] += 1

            if response.status_code != 200:
                print(f"Warning: Review comment search page {page} returned {response.status_code}", file=sys.stderr)
                break

            data = response.json()
            items = data.get('items', [])

            if not items:
                break  # Stop pagination if page is empty

            # Fetch review comments for all items on this page using the parent nursery
            async def fetch_and_store(item):
                result = await fetch_comments_for_item(client, item, username, days, False, True)
                if result:
                    all_results.append(result)

            for item in items:
                nursery.start_soon(fetch_and_store, item)

        except Exception as e:
            print(f"Error fetching review comment page {page}: {e}", file=sys.stderr)
            break

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
        print("No activity found in the last 7 days.")
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
        since = (datetime.now() - timedelta(days=7)).isoformat()
        since_date = since[:10]

        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }

        async with httpx.AsyncClient(headers=headers, limits=httpx.Limits(max_connections=20)) as client:
            all_results = []

            async with trio.open_nursery() as nursery:
                # Start PR activity and comment activity fetchers
                nursery.start_soon(get_pr_activity, client, nursery, username, 7, all_results, search_counter, since_date)
                nursery.start_soon(fetch_pr_comments, client, nursery, username, since_date, 7, all_results, search_counter)
                nursery.start_soon(fetch_issue_comments, client, nursery, username, since_date, 7, all_results, search_counter)
                nursery.start_soon(fetch_review_comments, client, nursery, username, since_date, 7, all_results, search_counter)

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
