"""Packaged native receipt policy with injected APIs; not Windows execution."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PWSH = os.environ.get("ASTINUSLAB_TEST_PWSH")


class ReceiptTests(unittest.TestCase):
    def test_native_contract_with_injected_api(self):
        if not PWSH:
            self.skipTest("ASTINUSLAB_TEST_PWSH required")
        from astinuslab.images.windows_bundle import parse_windows_bundle
        from astinuslab.virtualization.windows_prepared import native_source
        from tests.python._support.windows_images import windows_payload

        source_text = native_source(parse_windows_bundle(windows_payload()["first_boot"]))
        fake = r"""
using System;
using System.Collections.Generic;
using System.Text;
using UnattendedReceipt;
public class FakeApi : IApi {
 public string Scenario; public int Opened,Closed,Reads; public Dictionary<long,string> Paths=new Dictionary<long,string>();
 public byte[] Bytes=Encoding.UTF8.GetBytes("{\"schema_version\":1,\"nonce\":\"db3396d32fa84103b4f7b29c27179dbb\",\"stage\":\"specialize\",\"initialized\":true,\"enabled\":true,\"password_last_set_ticks\":\"639000000000000000\"}");
 public IntPtr Open(string path,int index,IntPtr parent){
  long expected=index==0?0:(index>=3?3:index);
  if(parent.ToInt64()!=expected)throw new Exception("parent_binding");
  if(Scenario=="open"&&index==3)throw new Exception();Opened++;Paths[Opened]=path;return new IntPtr(Opened);
 }
 public Metadata Info(IntPtr h,int index){
  var m=new Metadata{Attributes=(uint)(index<3?16:32),Links=1,Volume=123,FileId=(ulong)(index+1),Kind=1,Path=Paths[h.ToInt64()],Size=(ulong)Bytes.Length,Creation=1,Write=1,AclSafe=true};
  if(Scenario=="reparse"&&index==1)m.Attributes|=1024;
  if(Scenario=="links"&&index==4)m.Links=2;
  if(Scenario=="linkzero"&&index==4)m.Links=0;
  if(Scenario=="path"&&index==4)m.Path="C:\\unexpected";
  if(Scenario=="alias"&&index==4)m.FileId=4;
  if(Scenario=="volume"&&index==4)m.Volume=999;
  if(Scenario=="type"&&index==4)m.Attributes=16;
  if(Scenario=="kind"&&index==4)m.Kind=3;
  if(Scenario=="acl"&&index==2)m.AclSafe=false;
  if(Scenario=="oversize"&&index==4)m.Size=1025;
  if(Scenario=="empty"&&index==4)m.Size=0;
  if(Scenario=="changed"&&index==4&&Reads>0)m.Write=2;
  return m;
 }
 public byte[] Read(IntPtr h,int count){Reads++;if(Scenario=="short")return new byte[1];if(Scenario=="json")return Encoding.UTF8.GetBytes(new String('X',count));return (byte[])Bytes.Clone();}
 public bool Close(IntPtr h){Closed++;return !(Scenario=="close"&&Closed==1);}
}
"""
        harness = r"""param($Source,$Fake)
$ErrorActionPreference='Stop'
Add-Type -Path @($Source,$Fake)
$rows=@()
foreach($s in @('ok','open','reparse','links','linkzero','path','alias','volume','type','kind','acl','oversize','empty','changed','short','json','close')){
 $api=[FakeApi]::new();$api.Scenario=$s;$r=[UnattendedReceipt.Guard]::Collect($api)
 $rows+=@{scenario=$s;result=$r;opened=$api.Opened;closed=$api.Closed;reads=$api.Reads}
}
if([UnattendedReceipt.Guard]::CollectNative().Failure -cne 'platform'){throw 'native_platform'}
$rows|ConvertTo-Json -Depth 6 -Compress
"""
        with tempfile.TemporaryDirectory(prefix="astinuslab-receipt-test-") as root:
            source = Path(root) / "Native.cs"
            source.write_text(source_text)
            fp = Path(root) / "Fake.cs"
            fp.write_text(fake)
            hp = Path(root) / "harness.ps1"
            hp.write_text(harness)
            result = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(hp), str(source), str(fp)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr[:1800])
        self.assertEqual(result.stderr, "")
        for row in json.loads(result.stdout):
            with self.subTest(scenario=row["scenario"]):
                self.assertEqual(row["opened"], row["closed"])
                self.assertEqual(row["result"]["Success"], row["scenario"] == "ok")
                if row["scenario"] != "ok":
                    self.assertIsNone(row["result"]["PasswordLastSetTicks"])
                if row["scenario"] in ("reparse", "links", "acl", "oversize", "alias", "path"):
                    self.assertEqual(row["reads"], 0)


if __name__ == "__main__":
    unittest.main()
