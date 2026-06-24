import pandas as pd
import re


# -----------------------------
# Firm Configuration
# -----------------------------
FIRM_NAME = "dialsquare"

# -----------------------------
# Helpers
# -----------------------------
def normalize(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower()


def is_valid_guid(value):
    """Check if a value is a valid GUID format"""
    if pd.isna(value) or str(value).strip() == "" or str(value).lower() == "nan":
        return False
    # GUID pattern: 8-4-4-4-12 hexadecimal characters
    guid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    return bool(re.match(guid_pattern, str(value).lower().strip()))


# -----------------------------
# Load CSVs
# -----------------------------
emoney_df = pd.read_csv(f"{FIRM_NAME}_emoney_contacts.csv")
fyn_df = pd.read_csv(f"{FIRM_NAME}_fynancial_contacts.csv")

# -----------------------------
# Normalize key columns
# -----------------------------
# eMoney
emoney_df["contact_guid_norm"] = normalize(emoney_df["Contact GUID"])
emoney_df["contact_username_norm"] = normalize(emoney_df["Contact User Name"])
emoney_df["contact_name_norm"] = (
        normalize(emoney_df["Contact First Name"]) + "|" + normalize(emoney_df["Contact Last Name"])
)

# Fynancial
fyn_df["emoney_id_norm"] = normalize(fyn_df["Fynancial_EmoneyID"])
fyn_df["email_norm"] = normalize(fyn_df["Fynancial_Email"])
fyn_df["name_norm"] = (
        normalize(fyn_df["Fynancial_FirstName"]) + "|" + normalize(fyn_df["Fynancial_LastName"])
)

# -----------------------------
# Output scaffold
# -----------------------------
output_df = fyn_df.copy()
output_df["eMoney_Contact_Type"] = None
output_df["match_method"] = "No_match"
output_df["shares_emoneyid_with"] = None

# -----------------------------
# 1️⃣ Identity-first matching
# -----------------------------
for idx, row in output_df.iterrows():
    fyn_email = row["email_norm"]
    fyn_name = row["name_norm"]
    fyn_id = row["emoney_id_norm"]

    candidates = emoney_df.loc[emoney_df["contact_guid_norm"] == fyn_id]

    # --- Match by Email first ---
    email_match = candidates.loc[candidates["contact_username_norm"] == fyn_email]
    if len(email_match) == 1:
        match = email_match.iloc[0]
        output_df.at[idx, "eMoney_Contact_Type"] = match["Contact Type"]
        output_df.at[idx, "match_method"] = "eMoneyID_and_Email"
        continue

    # --- Match by Name within household ---
    name_match = candidates.loc[candidates["contact_name_norm"] == fyn_name]
    if len(name_match) == 1:
        match = name_match.iloc[0]
        output_df.at[idx, "eMoney_Contact_Type"] = match["Contact Type"]
        output_df.at[idx, "match_method"] = "eMoneyID_and_Name"
        continue

    # --- Email-only across all contacts ---
    email_only_match = emoney_df.loc[emoney_df["contact_username_norm"] == fyn_email]
    if len(email_only_match) == 1:
        match = email_only_match.iloc[0]
        output_df.at[idx, "eMoney_Contact_Type"] = match["Contact Type"]
        output_df.at[idx, "match_method"] = "Email_only"
        continue

    # --- Name-only across all contacts ---
    name_only_match = emoney_df.loc[emoney_df["contact_name_norm"] == fyn_name]
    if len(name_only_match) == 1:
        match = name_only_match.iloc[0]
        output_df.at[idx, "eMoney_Contact_Type"] = match["Contact Type"]
        output_df.at[idx, "match_method"] = "Name_only"
        continue

    if len(candidates) > 1:
        output_df.at[idx, "match_method"] = "Ambiguous_eMoneyID"
    else:
        output_df.at[idx, "match_method"] = "No_match"

# -----------------------------
# 2️⃣ Shared EmoneyID logic (uses normalized ID)
# -----------------------------
matched_users = (
    output_df.loc[
        (output_df["match_method"].isin(["eMoneyID_and_Email", "eMoneyID_and_Name"])) &
        (output_df["eMoney_Contact_Type"].isin(["FullClients", "Prospects"]))
        ]
    .groupby("emoney_id_norm")  # ✅ normalized
    .apply(lambda df: [f"{fn} {ln}" for fn, ln in zip(df["Fynancial_FirstName"], df["Fynancial_LastName"])])
    .to_dict()
)

# Apply shares_emoneyid_with for non-primary users
for idx, row in output_df.iterrows():
    fyn_id_norm = row["emoney_id_norm"]  # ✅ normalized
    if fyn_id_norm in matched_users:
        user_name = f"{row['Fynancial_FirstName']} {row['Fynancial_LastName']}"
        if user_name not in matched_users[fyn_id_norm]:
            output_df.at[idx, "shares_emoneyid_with"] = ", ".join(matched_users[fyn_id_norm])

# -----------------------------
# 3️⃣ isPrimary logic
# -----------------------------
output_df["isPrimary"] = "No"
output_df["Reason"] = ""

for idx, row in output_df.iterrows():
    fyn_emoneyid = row["Fynancial_EmoneyID"]
    match_method = row["match_method"]
    contact_type = row["eMoney_Contact_Type"]
    shares_with = row["shares_emoneyid_with"]

    # Check if no Fynancial_EmoneyID exists
    if pd.isna(fyn_emoneyid) or str(fyn_emoneyid).strip() == "" or str(fyn_emoneyid).lower() == "nan":
        output_df.at[idx, "isPrimary"] = ""
        output_df.at[idx, "Reason"] = "No eMoneyID mapped in Fynancial"
        continue

    # Check if Fynancial_EmoneyID is a valid GUID
    if not is_valid_guid(fyn_emoneyid):
        output_df.at[idx, "isPrimary"] = "Unknown"
        output_df.at[idx, "Reason"] = "Fynancial eMoneyID is not a valid GUID"
        continue

    # isPrimary = Yes conditions
    if (match_method in ["eMoneyID_and_Email", "eMoneyID_and_Name"]) and (contact_type in ["FullClients", "Prospects"]):
        output_df.at[idx, "isPrimary"] = "Yes"
        output_df.at[idx, "Reason"] = f"Matched on {match_method} and is {contact_type}"

    # isPrimary = No conditions
    else:
        reasons = []

        if match_method == "No_match" and pd.notna(shares_with) and shares_with != "":
            output_df.at[idx, "isPrimary"] = "No"
            reasons.append(f"No match found, but shares eMoney ID with: {shares_with}")
        elif match_method == "No_match":
            output_df.at[idx, "isPrimary"] = "Unknown"
            reasons.append("No match found in eMoney contacts")
        elif match_method in ["Email_only", "Name_only"]:
            if pd.notna(shares_with) and shares_with != "":
                output_df.at[idx, "isPrimary"] = "No"
                reasons.append(
                    f"Matched on {match_method} only, not through eMoney ID, shares eMoney ID with: {shares_with}")
            else:
                output_df.at[idx, "isPrimary"] = "Unknown"
                reasons.append(f"Matched on {match_method} only, not through eMoney ID")
        elif contact_type == "AddlLogon" and pd.notna(shares_with) and shares_with != "":
            output_df.at[idx, "isPrimary"] = "No"
            reasons.append(f"Matched as AddlLogon (not FullClients), shares eMoney ID with: {shares_with}")
        elif contact_type == "AddlLogon":
            output_df.at[idx, "isPrimary"] = "Unknown"
            reasons.append(f"Matched via {match_method}, is an AddlLogon, but does not share an eMoneyID with anyone")
        elif match_method == "Ambiguous_eMoneyID":
            output_df.at[idx, "isPrimary"] = "Unknown"
            reasons.append("Multiple contacts found with same eMoney ID")
        else:
            output_df.at[idx, "isPrimary"] = "Unknown"
            reasons.append("Does not meet primary criteria")

        output_df.at[idx, "Reason"] = "; ".join(reasons) if reasons else "Does not meet primary criteria"

# -----------------------------
# 4️⃣ Detect duplicate primaries (uses normalized ID)
# -----------------------------
primary_counts = output_df[output_df["isPrimary"] == "Yes"].groupby("emoney_id_norm").size()  # ✅ normalized
duplicate_primary_ids = primary_counts[primary_counts > 1].index.tolist()

for idx, row in output_df.iterrows():
    if row["emoney_id_norm"] in duplicate_primary_ids and row["isPrimary"] == "Yes":  # ✅ normalized
        original_reason = row["Reason"]
        output_df.at[idx, "isPrimary"] = "Unknown"
        output_df.at[idx, "Reason"] = f"Multiple users with same eMoney ID both marked as primary ({original_reason})"

# -----------------------------
# 5️⃣ Final column order
# -----------------------------
final_columns = [
    "Fynancial_UserUniqueId",
    "Fynancial_FirstName",
    "Fynancial_LastName",
    "Fynancial_Email",
    "Fynancial_EmoneyID",
    "isPrimary",
    "Reason",
]

final_df = output_df[final_columns]

# -----------------------------
# 6️⃣ Sort by EmoneyID and isPrimary
# -----------------------------
final_df["_sort_key"] = final_df["isPrimary"].map({"Yes": 0, "No": 1, "Unknown": 2, "": 3})
final_df = final_df.sort_values(by=["Fynancial_EmoneyID", "_sort_key"], na_position="last")
final_df = final_df.drop(columns=["_sort_key"])
final_df = final_df.reset_index(drop=True)

# -----------------------------
# Write CSV
# -----------------------------
output_filename = f"{FIRM_NAME}_fynancial_emoney_contact_mapping.csv"
final_df.to_csv(output_filename, index=False)

assert len(final_df) == len(fyn_df), "Row count mismatch!"
print(f"✅ CSV generated: {output_filename}")
print(f"\nSummary:")
print(f"  Total records: {len(final_df)}")
print(f"  isPrimary = Yes: {len(final_df[final_df['isPrimary'] == 'Yes'])}")
print(f"  isPrimary = No: {len(final_df[final_df['isPrimary'] == 'No'])}")
print(f"  isPrimary = Unknown: {len(final_df[final_df['isPrimary'] == 'Unknown'])}")
print(f"  isPrimary = (empty): {len(final_df[final_df['isPrimary'] == ''])}")