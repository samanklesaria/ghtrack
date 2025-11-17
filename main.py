#!/usr/bin/env python3
import os
import sys
from datetime import datetime, timedelta
from dateutil import parser as date_parser
import requests

def get_pr_activity(token, username, days=7):
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    since = (datetime.now() - timedelta(days=days)).isoformat()
    since_date = since[:10]

    # Search for PRs where user is author
    search_url = 'https://api.github.com/search/issues'
    query = f'is:pr author:{username} updated:>={since_date}'
    params = {'q': query, 'per_page': 100, 'sort': 'updated', 'order': 'desc'}

    pr_activity = {}
    page = 1

    while page <= 10:
        params['page'] = page
        response = requests.get(search_url, headers=headers, params=params)

        if response.status_code != 200:
            print(f"Warning: PR search returned {response.status_code}", file=sys.stderr)
            break

        data = response.json()
        prs = data.get('items', [])

        if not prs:
            break

        for pr in prs:
            repo = pr['repository_url'].replace('https://api.github.com/repos/', '')
            pr_number = pr['number']

            commits_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/commits"
            commits_response = requests.get(commits_url, headers=headers)

            if commits_response.status_code != 200:
                continue

            commits = commits_response.json()
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
                pr_activity[pr_key] = {
                    'repo': repo,
                    'number': pr_number,
                    'title': pr['title'],
                    'url': pr['html_url'],
                    'state': pr['state'],
                    'is_author': True,
                    'commits': user_commits,
                    'comments': []
                }

        page += 1

    return pr_activity

def get_comment_activity(token, username, days=7):
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    since = (datetime.now() - timedelta(days=days)).isoformat()
    since_date = since[:10]

    activity = {}

    # Search for PRs where user commented
    search_url = 'https://api.github.com/search/issues'
    query = f'is:pr commenter:{username} updated:>={since_date} -author:{username}'
    params = {'q': query, 'per_page': 100, 'sort': 'updated', 'order': 'desc'}

    page = 1
    while page <= 10:
        params['page'] = page
        response = requests.get(search_url, headers=headers, params=params)

        if response.status_code != 200:
            print(f"Warning: Comment search returned {response.status_code}", file=sys.stderr)
            break

        data = response.json()
        items = data.get('items', [])

        if not items:
            break

        for item in items:
            repo = item['repository_url'].replace('https://api.github.com/repos/', '')
            number = item['number']

            comments_url = item['comments_url']
            comments_response = requests.get(comments_url, headers=headers)

            if comments_response.status_code != 200:
                continue

            comments = comments_response.json()
            user_comments = []

            for comment in comments:
                if comment['user']['login'] == username:
                    comment_date = date_parser.isoparse(comment['created_at'])
                    if comment_date >= datetime.now(comment_date.tzinfo) - timedelta(days=days):
                        user_comments.append({
                            'date': comment_date,
                            'body': comment['body'][:100] + ('...' if len(comment['body']) > 100 else '')
                        })

            if user_comments:
                key = f"{repo}#{number}"
                activity[key] = {
                    'repo': repo,
                    'number': number,
                    'title': item['title'],
                    'url': item['html_url'],
                    'state': item['state'],
                    'is_author': False,
                    'comments': user_comments
                }

        page += 1

    # Search for issues where user commented
    query = f'is:issue commenter:{username} updated:>={since_date} -author:{username}'
    params = {'q': query, 'per_page': 100, 'sort': 'updated', 'order': 'desc'}

    page = 1
    while page <= 10:
        params['page'] = page
        response = requests.get(search_url, headers=headers, params=params)

        if response.status_code != 200:
            break

        data = response.json()
        items = data.get('items', [])

        if not items:
            break

        for item in items:
            repo = item['repository_url'].replace('https://api.github.com/repos/', '')
            number = item['number']

            comments_url = item['comments_url']
            comments_response = requests.get(comments_url, headers=headers)

            if comments_response.status_code != 200:
                continue

            comments = comments_response.json()
            user_comments = []

            for comment in comments:
                if comment['user']['login'] == username:
                    comment_date = date_parser.isoparse(comment['created_at'])
                    if comment_date >= datetime.now(comment_date.tzinfo) - timedelta(days=days):
                        user_comments.append({
                            'date': comment_date,
                            'body': comment['body'][:100] + ('...' if len(comment['body']) > 100 else '')
                        })

            if user_comments:
                key = f"{repo}#{number}"
                activity[key] = {
                    'repo': repo,
                    'number': number,
                    'title': item['title'],
                    'url': item['html_url'],
                    'state': item['state'],
                    'is_author': False,
                    'is_issue': True,
                    'comments': user_comments
                }

        page += 1

    return activity

def generate_report(pr_activity, comment_activity, username):
    # Collect all activity by date
    activity_by_date = {}
    url_to_title = {}

    for pr in pr_activity.values():
        url_to_title[pr['url']] = pr['title']
        for commit in pr['commits']:
            date = commit['date'].strftime('%Y-%m-%d')
            if date not in activity_by_date:
                activity_by_date[date] = set()
            activity_by_date[date].add(pr['url'])

    for item in comment_activity.values():
        url_to_title[item['url']] = item['title']
        for comment in item['comments']:
            date = comment['date'].strftime('%Y-%m-%d')
            if date not in activity_by_date:
                activity_by_date[date] = set()
            activity_by_date[date].add(item['url'])

    if not activity_by_date:
        print("No activity found in the last 7 days.")
        return

    # Print by day
    for date in sorted(activity_by_date.keys(), reverse=True):
        day_name = datetime.strptime(date, '%Y-%m-%d').strftime('%A')
        print(f"# {day_name} ({date})")
        for url in sorted(activity_by_date[date]):
            title = url_to_title.get(url, "")
            print(f"- {url} - {title}")
        print()

if __name__ == '__main__':
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

    # Validate credentials
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json'
    }
    try:
        auth_response = requests.get('https://api.github.com/user', headers=headers)
        if auth_response.status_code == 401:
            print("Error: Invalid GitHub credentials", file=sys.stderr)
            print("The provided GITHUB_TOKEN is not valid or has expired.", file=sys.stderr)
            print("\nCreate a new token at: https://github.com/settings/tokens", file=sys.stderr)
            print("Required scopes: 'repo' (or 'public_repo' for public repos only)", file=sys.stderr)
            sys.exit(1)
        elif auth_response.status_code != 200:
            print(f"Error: GitHub API returned status {auth_response.status_code}", file=sys.stderr)
            sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Error validating credentials: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        pr_activity = get_pr_activity(token, username)
        comment_activity = get_comment_activity(token, username)
        generate_report(pr_activity, comment_activity, username)
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
