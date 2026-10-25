"""Executable C# policy with injected NTFS calls; not native Windows proof."""

import json
import os
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
SOURCE = ROOT / "tools/dev/windows_candidate_trial/NativeAnswerRestriction.cs"


@pytest.mark.parametrize(
    "source", [SOURCE, ROOT / "src/astinuslab/virtualization/windows_assets/NativeAnswerRestriction.cs"]
)
def test_restriction_is_bound_single_write_verified_and_fail_closed(tmp_path, source):
    pwsh = os.environ.get("ASTINUSLAB_TEST_PWSH")
    if not pwsh:
        pytest.skip("set ASTINUSLAB_TEST_PWSH for executable C# policy checks")
    assert source.exists(), "post-Setup answer restriction missing"
    fake = tmp_path / "Fake.cs"
    fake.write_text(r"""
using System;
using AstinusAnswer;
public class RestrictionFake : IRestrictionApi {
 public string Scenario,InvalidSecurity; public int Opens,Closes,Infos,Sets; public byte[] Written;
 public IApi ReadOnly { get { return this; } }
 public const string Target="37892|S-1-5-18|S-1-5-32-544|0,0,2032127,S-1-5-18;0,0,2032127,S-1-5-32-544;";
 public const string Offline="36868|S-1-5-18|S-1-5-32-544|0,0,2032127,S-1-5-18;0,0,2032127,S-1-5-32-544;";
 public const string Cached="33796|S-1-5-18|S-1-5-18|0,16,2032127,S-1-5-18;0,16,2032127,S-1-5-32-544;0,16,1179817,S-1-5-32-545;0,16,1179817,S-1-15-2-1;0,16,1179817,S-1-15-2-2;";
 public IntPtr Open(string p,int i,IntPtr parent){
  if(p!=Guard.Paths[i] || parent.ToInt64()!=(i==0?0:i))throw new Exception("binding");
  if(Scenario=="absent"&&i==3)return IntPtr.Zero;
  Opens++;return new IntPtr(i+1);
 }
 public Metadata Info(IntPtr h,int i){
  Infos++;var m=new Metadata{Kind=1,Volume=123,FileId=(ulong)(i+5),Attributes=(uint)(i<3?16:32),
   Links=1,Size=(ulong)(i==3?1392:4096),Creation=1,Write=1,Path=Guard.Paths[i],
   Security=i<3?"acl"+i:(Sets>0||Scenario=="idempotent"?Target:Cached),DescriptorHash=new string('a',64)};
  if(Scenario=="parent_acl"&&i==1)m.Security="wrong";
  if(Scenario=="parent_type"&&i==1)m.Attributes=32;
  if(Scenario=="reparse"&&i==3)m.Attributes|=1024;
  if(Scenario=="links"&&i==3)m.Links=2;
  if(Scenario=="alias"&&i==3)m.FileId=5;
  if(Scenario=="wrong_acl"&&i==3)m.Security="wrong";
  if(Scenario=="changed_before"&&i==3&&Infos>4)m.Write=2;
  if(Scenario=="changed_after"&&i==3&&Sets>0)m.Write=2;
  if(Scenario=="still_broad"&&i==3&&Sets>0)m.Security=Cached;
  if(Scenario=="offline_initial"&&i==3&&Sets==0)m.Security=Offline;
  if(Scenario=="offline_postset"&&i==3&&Sets>0)m.Security=Offline;
  if(i==3 && (Scenario=="invalid_initial" || (Scenario=="invalid_held"&&Sets>0) ||
     (Scenario=="invalid_reopen"&&Opens>4)))m.Security=InvalidSecurity;
  if(Scenario=="replaced_reopen"&&i==3&&Opens>4)m.FileId=88;
  return m;
 }
 public uint Set(IntPtr h,byte[] bytes){
  if(h.ToInt64()!=4||Closes!=0||Infos<8)throw new Exception("early_write");
  Sets++;Written=bytes;return Scenario=="setter_error"?5u:0u;
 }
 public bool Close(IntPtr h){Closes++;return Scenario!="close";}
}
""")
    harness = tmp_path / "check.ps1"
    # These mutations catch masking control bits or relaxing owner/group/ACE
    # equality at any boundary. Expectations are independent of production policy.
    target = "37892|S-1-5-18|S-1-5-32-544|0,0,2032127,S-1-5-18;0,0,2032127,S-1-5-32-544;"
    invalid = {
        "no_dacl": target.replace("37892|", "37888|"),
        "unprotected": target.replace("37892|", "33796|"),
        "owner_defaulted": target.replace("37892|", "37893|"),
        "group_defaulted": target.replace("37892|", "37894|"),
        "dacl_defaulted": target.replace("37892|", "37900|"),
        "auto_request": target.replace("37892|", "38148|"),
        "extra_control": target.replace("37892|", "54276|"),
        "absolute": target.replace("37892|", "5124|"),
        "owner": target.replace("|S-1-5-18|", "|S-1-5-32-544|"),
        "group": target.replace("|S-1-5-32-544|", "|S-1-5-18|"),
        "extra_ace": target + "0,0,1179817,S-1-5-32-545;",
        "empty_acl": "37892|S-1-5-18|S-1-5-32-544|",
        "missing_ace": target.removesuffix("0,0,2032127,S-1-5-32-544;"),
        "inherited_ace": target.replace("0,0,2032127", "0,16,2032127", 1),
        "object_inherit": target.replace("0,0,2032127", "0,1,2032127", 1),
        "container_inherit": target.replace("0,0,2032127", "0,2,2032127", 1),
        "inherit_only": target.replace("0,0,2032127", "0,8,2032127", 1),
        "deny": target.replace("0,0,2032127", "1,0,2032127", 1),
        "callback": target.replace("0,0,2032127", "9,0,2032127", 1),
        "mask": target.replace("2032127", "1179817", 1),
        "users": target.replace("2032127,S-1-5-18;", "2032127,S-1-5-32-545;"),
        "app_package": target.replace("2032127,S-1-5-18;", "2032127,S-1-15-2-1;"),
        "duplicate": target.replace("2032127,S-1-5-32-544;", "2032127,S-1-5-18;"),
        "order": "37892|S-1-5-18|S-1-5-32-544|0,0,2032127,S-1-5-32-544;0,0,2032127,S-1-5-18;",
    }
    mutations = tmp_path / "mutations.json"
    mutations.write_text(json.dumps(invalid))
    harness.write_text(r"""param($Reader,$Restriction,$Fake,$Mutations)
$ErrorActionPreference='Stop';Add-Type -Path @($Reader,$Restriction,$Fake)
$rows=@()
foreach($s in @('ok','idempotent','absent','parent_acl','parent_type','reparse','links','alias','wrong_acl',
 'changed_before','changed_after','still_broad','offline_initial','offline_postset','replaced_reopen','setter_error','close','setup')) {
 $api=[RestrictionFake]::new();$api.Scenario=$s
 $ready=[Func[bool]]{return $s -cne 'setup'}
 $r=[AstinusAnswer.Restriction]::Apply($api,[string[]]@('acl0','acl1','acl2'),$ready)
 $rows+=@{scenario=$s;result=$r;sets=$api.Sets;opened=$api.Opens;closed=$api.Closes;
  written=$(if($null -eq $api.Written){$null}else{[Convert]::ToBase64String($api.Written)})}
}
foreach($entry in (Get-Content -LiteralPath $Mutations -Raw|ConvertFrom-Json).PSObject.Properties) {
 foreach($phase in @('initial','held','reopen')) {
  $api=[RestrictionFake]::new();$api.Scenario='invalid_'+$phase;$api.InvalidSecurity=$entry.Value
  $r=[AstinusAnswer.Restriction]::Apply($api,[string[]]@('acl0','acl1','acl2'),[Func[bool]]{return $true})
  $rows+=@{scenario=$phase+':'+$entry.Name;result=$r;sets=$api.Sets;opened=$api.Opens;closed=$api.Closes}
 }
}
$rows|ConvertTo-Json -Depth 6 -Compress
""")
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            str(source.with_name("NativeAnswer.cs")),
            str(source),
            str(fake),
            str(mutations),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    rows = {r["scenario"]: r for r in json.loads(completed.stdout)}
    assert len(rows) == 18 + 3 * len(invalid)
    written_cases = {
        "ok",
        "changed_after",
        "still_broad",
        "offline_initial",
        "offline_postset",
        "replaced_reopen",
        "setter_error",
        "close",
    }
    for case, row in rows.items():
        written = case in written_cases or case.startswith(("held:", "reopen:"))
        assert row["result"]["Success"] is (case in {"ok", "idempotent", "offline_initial"}), row
        assert row["sets"] == int(written), row
        assert row["result"]["MutationAttempted"] is written, row
        assert row["opened"] == row["closed"], row
        if ":" in case:
            phase, mutation = case.split(":")
            assert row["result"]["ObservedSecurity"] == invalid[mutation], row
            assert row["result"]["Phase"] == {"initial": "open", "held": "after_set", "reopen": "reopen"}[phase]
            assert row["result"]["Failure"] == ("cached_acl" if phase == "initial" else "acl_3"), row
    assert rows["parent_type"]["opened"] == 2
    assert rows["ok"]["result"]["ReopenedVerified"] is True
    assert rows["idempotent"]["result"]["ReopenedVerified"] is True
    assert rows["replaced_reopen"]["result"]["Failure"] == "changed"
    assert rows["setter_error"]["result"]["SetterError"] == 5
    assert rows["still_broad"]["result"]["Failure"] == "acl_3"
    assert rows["still_broad"]["result"].get("ObservedSecurity") == (
        "33796|S-1-5-18|S-1-5-18|0,16,2032127,S-1-5-18;0,16,2032127,S-1-5-32-544;"
        "0,16,1179817,S-1-5-32-545;0,16,1179817,S-1-15-2-1;0,16,1179817,S-1-15-2-2;"
    )
    assert rows["absent"]["result"]["HeldVerified"] is False
    # Win32 native post-set verification requires exactly 0x9404; an unchanged
    # offline 0x9004 result is not silently accepted as a native setter pass.
    assert rows["ok"]["result"]["Leaf"]["Security"] == target
    assert rows["offline_postset"]["result"]["Failure"] == "acl_3"
    assert rows["offline_postset"]["result"]["ObservedSecurity"] == target.replace("37892|", "36868|")

    # Independently decode the actual descriptor sent to the API boundary.
    import base64

    raw = base64.b64decode(rows["ok"]["written"], validate=True)
    assert len(raw) == 100
    assert struct.unpack_from("<BBHIIII", raw) == (1, 0, 0x9004, 72, 84, 0, 20)
    assert struct.unpack_from("<BBHHH", raw, 20) == (2, 0, 52, 2, 0)
    assert raw[28:48].hex() == "00001400ff011f00010100000000000512000000"
    assert raw[48:72].hex() == "00001800ff011f0001020000000000052000000020020000"
    assert raw[72:] == raw[36:48] + raw[56:72]
