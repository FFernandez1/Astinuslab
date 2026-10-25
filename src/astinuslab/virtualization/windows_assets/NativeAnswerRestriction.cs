using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace AstinusAnswer {
 public interface IRestrictionApi : IApi {
  IApi ReadOnly { get; }
  uint Set(IntPtr handle,byte[] descriptor);
 }
 public sealed class RestrictionResult {
  public bool Success,MutationAttempted,HeldVerified,ReopenedVerified;
  public string Phase="arguments",Failure="none",ObservedSecurity;
  public uint? SetterError;
  public int Opened,Closed,CloseFailures;
  public Metadata Leaf;
 }
 public static class Restriction {
  // Separate exact representations of the same protected SYSTEM/Admin policy.
  // SetSecurityInfo takes SID/ACL pointers, not the source descriptor header;
  // the measured Server 2022 native result includes SE_DACL_AUTO_INHERITED.
  // SE_DACL_PROTECTED and both explicit ACEs remain mandatory. Never mask bits
  // or learn policy from an arbitrary current descriptor. Only OWNER/GROUP/DACL
  // are queried; neither representation makes a SACL claim.
  // https://learn.microsoft.com/en-us/windows/win32/secauthz/automatic-propagation-of-inheritable-aces
  public const string OfflineTarget="36868|S-1-5-18|S-1-5-32-544|0,0,2032127,S-1-5-18;0,0,2032127,S-1-5-32-544;";
  public const string NativeTarget="37892|S-1-5-18|S-1-5-32-544|0,0,2032127,S-1-5-18;0,0,2032127,S-1-5-32-544;";
  public const string Cached="33796|S-1-5-18|S-1-5-18|0,16,2032127,S-1-5-18;0,16,2032127,S-1-5-32-544;0,16,1179817,S-1-5-32-545;0,16,1179817,S-1-15-2-1;0,16,1179817,S-1-15-2-2;";
  internal static byte[] Descriptor() {
   const string hex="0100049048000000540000000000000014000000020034000200000000001400ff011f0001010000000000051200000000001800ff011f000102000000000005200000002002000001010000000000051200000001020000000000052000000020020000";
   var bytes=new byte[100];for(int i=0;i<bytes.Length;i++)bytes[i]=Convert.ToByte(hex.Substring(i*2,2),16);return bytes;
  }
  static void Check(bool value,string code) { if(!value)throw new Failure(code); }
  static void Same(Metadata a,Metadata b) {
   Check(a!=null && b!=null && a.Kind==b.Kind && a.Volume==b.Volume && a.FileId==b.FileId &&
    a.Attributes==b.Attributes && a.Links==b.Links && a.Size==b.Size && a.Creation==b.Creation &&
    a.Write==b.Write && a.Path==b.Path,"changed");
  }
  public static RestrictionResult ApplyNative(string[] parents,Func<bool> ready) {
   try { return Apply(new NativeRestrictionApi(),parents,ready); }
   catch(Failure e) { return new RestrictionResult{Failure=e.Code}; }
   catch(Exception) { return new RestrictionResult{Failure="unexpected"}; }
  }
  public static RestrictionResult Apply(IRestrictionApi api,string[] parents,Func<bool> ready) {
   var r=new RestrictionResult();var handles=new List<IntPtr>();var initial=new List<Metadata>();
   var ids=new HashSet<ulong>();var clock=Stopwatch.StartNew();var expected=new string[4];
   uint volume=0;bool heldComplete=false;var audit=new Result();
   try {
    Check(api!=null && parents!=null && parents.Length==3 && ready!=null,"arguments");
    for(int i=0;i<3;i++) { Check(parents[i]!=null && parents[i].Length<=8192,"expected");expected[i]=parents[i]; }
    r.Phase="open";
    for(int i=0;i<4;i++) {
     Check(clock.ElapsedMilliseconds<10000,"deadline");
     IntPtr h=api.Open(Guard.Paths[i],i,i==0?IntPtr.Zero:handles[i-1]);
     Check(h!=IntPtr.Zero && h!=new IntPtr(-1),"open");handles.Add(h);r.Opened++;
     var m=api.Info(h,i);Check(m!=null,"metadata");if(i==0)volume=m.Volume;
     if(i==3) {
      if(m.Security!=Cached && m.Security!=OfflineTarget && m.Security!=NativeTarget)r.ObservedSecurity=m.Security;
      Check(m.Security==Cached || m.Security==OfflineTarget || m.Security==NativeTarget,"cached_acl");expected[i]=m.Security;
     }
     Guard.Validate(m,i,volume,expected[i],audit);
     Check(ids.Add(m.FileId),"alias");initial.Add(m);
    }
    r.Phase="before_set";
    for(int i=0;i<4;i++) {
     var m=api.Info(handles[i],i);Guard.Validate(m,i,volume,expected[i],audit);Same(initial[i],m);
    }
    Check(ready(),"setup");Check(clock.ElapsedMilliseconds<10000,"deadline");
    if(initial[3].Security!=NativeTarget) {
     r.Phase="set";r.MutationAttempted=true;
     r.SetterError=api.Set(handles[3],Descriptor());Check(r.SetterError==0,"setter");
    }
    r.Phase="after_set";expected[3]=NativeTarget;
    for(int i=0;i<4;i++) {
     var m=api.Info(handles[i],i);Guard.Validate(m,i,volume,expected[i],audit);Same(initial[i],m);
     if(i==3)r.Leaf=m;
    }
    r.HeldVerified=true;heldComplete=true;
   } catch(Failure e) { r.Failure=e.Code;if(audit.ObservedSecurity!=null)r.ObservedSecurity=audit.ObservedSecurity; }
   catch(Exception) { r.Failure="unexpected"; }
   finally {
    for(int i=handles.Count-1;i>=0;i--) {
     bool closed=false;try { closed=api.Close(handles[i]); } catch(Exception) {}
     if(closed)r.Closed++;else r.CloseFailures++;
    }
   }
   if(r.CloseFailures!=0) { heldComplete=false;r.Failure="close";r.Phase="close"; }
   if(heldComplete) {
    try {
     r.Phase="reopen";Check(ready(),"setup");Check(clock.ElapsedMilliseconds<10000,"deadline");
     // Reacquire via the unchanged read-only acceptance guard after closing
     // every mutation handle. A swapped inode must still fail, even if ACL-correct.
     Result read=Guard.Collect(api.ReadOnly,expected);
     r.Opened+=read.Opened;r.Closed+=read.Closed;r.CloseFailures+=read.CloseFailures;
     r.ObservedSecurity=read.ObservedSecurity;
     Check(read.Success && read.LeafAclVerified,read.Failure=="none"?"reopen_absent":read.Failure);
     Same(r.Leaf,read.Leaf);r.ReopenedVerified=true;r.Success=true;r.Phase="complete";
    } catch(Failure e) { r.Failure=e.Code; } catch(Exception) { r.Failure="unexpected"; }
   }
   clock.Stop();return r;
  }
 }
 public sealed class NativeRestrictionApi : IRestrictionApi {
  readonly NativeApi api=new NativeApi();
  public IApi ReadOnly { get { return api; } }
  public IntPtr Open(string path,int index,IntPtr parent) { return api.OpenForRestriction(path,index,parent); }
  public Metadata Info(IntPtr handle,int index) { return api.Info(handle,index); }
  public bool Close(IntPtr handle) { return api.Close(handle); }
  [DllImport("advapi32.dll",ExactSpelling=true)] static extern uint SetSecurityInfo(IntPtr handle,int kind,uint flags,IntPtr owner,IntPtr group,IntPtr dacl,IntPtr sacl);
  public uint Set(IntPtr handle,byte[] descriptor) {
   if(handle==IntPtr.Zero || handle==new IntPtr(-1) || descriptor==null ||
      Convert.ToBase64String(descriptor)!=Convert.ToBase64String(Restriction.Descriptor()))throw new Failure("descriptor");
   var pinned=GCHandle.Alloc(descriptor,GCHandleType.Pinned);
   try {
    IntPtr p=pinned.AddrOfPinnedObject();
    // SE_FILE_OBJECT; OWNER/GROUP/DACL + PROTECTED_DACL. No SACL, privilege
    // adjustment, MAXIMUM_ALLOWED, path setter, file data write or deletion.
    return SetSecurityInfo(handle,1,0x80000007u,IntPtr.Add(p,72),IntPtr.Add(p,84),IntPtr.Add(p,20),IntPtr.Zero);
   } finally { pinned.Free(); }
  }
 }
}
