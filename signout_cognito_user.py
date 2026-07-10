import csv
import boto3
from botocore.exceptions import ClientError

# Configuration
USER_POOL_ID = "ap-south-1_hEhM0YHEw"
CSV_FILE = "recipients.csv"
AWS_REGION = "ap-south-1"

cognito = boto3.client("cognito-idp", region_name=AWS_REGION)

with open(CSV_FILE, newline="") as csvfile:
    reader = csv.DictReader(csvfile)

    for row in reader:
        email = row["email"].strip()

        try:
            cognito.admin_user_global_sign_out(
                UserPoolId=USER_POOL_ID,
                Username=email
            )
            print(f"✓ Signed out: {email}")

        except cognito.exceptions.UserNotFoundException:
            print(f"✗ User not found: {email}")

        except ClientError as e:
            print(f"✗ Failed for {email}: {e.response['Error']['Message']}")

print("Done.")