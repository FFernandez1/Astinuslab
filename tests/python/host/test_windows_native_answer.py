"""Execute the C# handle/ACL policy with an injected API; not native Windows."""

import json
import os
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[3] / "tools/dev/windows_candidate_trial/NativeAnswer.cs"


@pytest.mark.parametrize(
    "source", [SOURCE, Path(__file__).parents[3] / "src/astinuslab/virtualization/windows_assets/NativeAnswer.cs"]
)
def test_native_answer_guard_closes_handles_and_distinguishes_absence(tmp_path, source):
    pwsh = os.environ.get("ASTINUSLAB_TEST_PWSH")
    if not pwsh:
        pytest.skip("set ASTINUSLAB_TEST_PWSH for executable C# policy checks")
    assert source.exists(), "native answer-file guard missing"
    fake = tmp_path / "Fake.cs"
    fake.write_text(r"""
using System;
using AstinusAnswer;
public class FakeApi : IApi {
 public string Scenario; public int Opens,Closes,Infos;
 public IntPtr Open(string path,int i,IntPtr parent){
  if(parent.ToInt64()!=(i==0?0:i))throw new Exception("parent binding");
  if(Scenario=="missing"&&i==3)return IntPtr.Zero;
  if(Scenario=="open"&&i==2)throw new Failure("open");
  Opens++;return new IntPtr(i+1);
 }
 public Metadata Info(IntPtr h,int i){
  Infos++;var m=new Metadata{Kind=1,Volume=123,FileId=(ulong)(i+5),Attributes=(uint)(i<3?16:32),
   Links=1,Size=(ulong)(i==3?1392:4096),Creation=1,Write=1,Path=Guard.Paths[i],Security="acl"+i,
   DescriptorHash=new String('a',64)};
  if(Scenario=="reparse"&&i==1)m.Attributes|=1024;
  if(Scenario=="recall"&&i==3)m.Attributes|=0x40000;
  if(Scenario=="kind"&&i==3)m.Kind=3;
  if(Scenario=="type"&&i==3)m.Attributes=16;
  if(Scenario=="parent_type"&&i==1)m.Attributes=32;
  if(Scenario=="path"&&i==2)m.Path=@"C:\elsewhere";
  if(Scenario=="volume"&&i==3)m.Volume=2;
  if(Scenario=="alias"&&i==3)m.FileId=5;
  if(Scenario=="links"&&i==3)m.Links=2;
  if(Scenario=="size"&&i==3)m.Size=99999;
  if(Scenario=="acl"&&i==3)m.Security="wrong";
  if(Scenario=="parent_acl"&&i==1)m.Security="wrong";
  if(Scenario=="changed"&&Infos>4&&i==3)m.Write=2;
  return m;
 }
 public bool Close(IntPtr h){Closes++;return Scenario!="close";}
}
""")
    harness = tmp_path / "check.ps1"
    harness.write_text(r"""param($Source,$Fake)
$ErrorActionPreference='Stop';Add-Type -Path @($Source,$Fake)
$rows=@()
foreach($s in @('ok','missing','open','reparse','recall','kind','type','parent_type','path','volume','alias','links','size','acl','parent_acl','changed','close')){
 $api=[FakeApi]::new();$api.Scenario=$s
 $r=[AstinusAnswer.Guard]::Collect($api,[string[]]@('acl0','acl1','acl2','acl3'))
 $rows+=@{scenario=$s;result=$r;opened=$api.Opens;closed=$api.Closes}
}
if([AstinusAnswer.Guard]::CollectNative([string[]]@('a','b','c','d')).Failure -cne 'platform'){throw 'platform'}
$rows|ConvertTo-Json -Depth 6 -Compress
""")
    result = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(harness), str(source), str(fake)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert len(rows) == 17
    for row in rows:
        assert row["opened"] == row["closed"], row
        assert row["result"]["Success"] is (row["scenario"] in ("ok", "missing")), row
        if row["scenario"] == "missing":
            assert row["result"]["Status"] == "absent_leaf"
            assert row["result"]["LeafAclVerified"] is False
        if row["scenario"] == "ok":
            assert row["result"]["Status"] == "present"
            assert row["result"]["LeafAclVerified"] is True
        if row["scenario"] == "acl":
            assert row["result"]["Failure"] == "acl_3"
        if row["scenario"] == "parent_acl":
            assert row["result"]["Failure"] == "acl_1"
        if row["scenario"] == "parent_type":
            assert row["result"]["Failure"] == "type"
            assert row["opened"] == 2  # No child open beneath a non-directory.
