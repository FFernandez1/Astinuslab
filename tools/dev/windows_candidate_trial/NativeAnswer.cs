using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Text;

namespace AstinusAnswer {
 public sealed class Failure : Exception {
  public string Code;
  public Failure(string code) { Code=code; }
 }
 public sealed class Metadata {
  public uint Kind,Volume,Attributes,Links;
  public ulong FileId,Size,Creation,Write;
  public string Path,Security,DescriptorHash;
 }
 public interface IApi {
  // Zero is allowed only for the absent final component, never an absent parent.
  IntPtr Open(string path,int index,IntPtr parent);
  Metadata Info(IntPtr handle,int index);
  bool Close(IntPtr handle);
 }
 public sealed class Result {
  public Metadata Leaf;
  public bool Success,LeafAclVerified;
  public string Status="unknown",Failure="none",DescriptorHash,ObservedSecurity;
  public int Opened,Closed,CloseFailures;
 }
 public static class Guard {
  public static readonly string[] Paths={@"C:\",@"C:\Windows",@"C:\Windows\Panther",@"C:\Windows\Panther\unattend.xml"};
  static void Check(bool value,string code) { if(!value)throw new Failure(code); }
  internal static void Validate(Metadata m,int i,uint volume,string expected,Result result) {
   Check(m!=null && m.Kind==1,"kind");
   Check((m.Attributes&1024)==0,"reparse");
   Check((m.Attributes&(0x1000u|0x40000u|0x400000u))==0,"offline_or_recall");
   Check(((m.Attributes&16)!=0)==(i<3),"type");
   Check(String.Equals(m.Path,Paths[i],StringComparison.OrdinalIgnoreCase),"path");
   Check(m.Volume==volume && m.FileId!=0,"identity");
   if(i==3) { Check(m.Links==1,"links");Check(m.Size<=16384,"size"); }
   Check(m.Security!=null && m.Security.Length<=8192,"acl_bound");
   if(m.Security!=expected) { result.ObservedSecurity=m.Security;throw new Failure("acl_"+i.ToString(CultureInfo.InvariantCulture)); }
  }
  public static Result CollectNative(string[] expected) {
   try { return Collect(new NativeApi(),expected); }
   catch(Failure e) { return new Result{Failure=e.Code}; }
   catch(Exception) { return new Result{Failure="unexpected"}; }
  }
  public static Result Collect(IApi api,string[] expected) {
   var r=new Result();var handles=new List<IntPtr>();var initial=new List<Metadata>();
   var ids=new HashSet<ulong>();var clock=Stopwatch.StartNew();uint volume=0;
   try {
    Check(expected!=null && expected.Length==4,"expected");
    for(int i=0;i<4;i++) {
     Check(expected[i]!=null && expected[i].Length<=8192,"expected");
     Check(clock.ElapsedMilliseconds<5000,"deadline");
     IntPtr h=api.Open(Paths[i],i,i==0?IntPtr.Zero:handles[i-1]);
     if(h==IntPtr.Zero && i==3) { r.Status="absent_leaf";break; }
     Check(h!=IntPtr.Zero && h!=new IntPtr(-1),"open");handles.Add(h);r.Opened++;
     var m=api.Info(h,i);if(i==0) { Check(m!=null,"metadata");volume=m.Volume; }
     Validate(m,i,volume,expected[i],r);Check(ids.Add(m.FileId),"alias");initial.Add(m);
    }
    for(int i=0;i<handles.Count;i++) {
     Check(clock.ElapsedMilliseconds<5000,"deadline");var m=api.Info(handles[i],i);Validate(m,i,volume,expected[i],r);
     Check(m.FileId==initial[i].FileId && m.Attributes==initial[i].Attributes,"changed");
     if(i==3)Check(m.Links==initial[i].Links && m.Size==initial[i].Size && m.Creation==initial[i].Creation && m.Write==initial[i].Write,"changed");
    }
    if(handles.Count==4) { r.Status="present";r.LeafAclVerified=true;r.DescriptorHash=initial[3].DescriptorHash;r.Leaf=initial[3]; }
    Check(handles.Count==4 || (handles.Count==3 && r.Status=="absent_leaf"),"incomplete");r.Success=true;
   } catch(Failure e) { r.Failure=e.Code; } catch(Exception) { r.Failure="unexpected"; }
   finally {
    for(int i=handles.Count-1;i>=0;i--) {
     bool closed=false;try { closed=api.Close(handles[i]); } catch(Exception) {}
     if(closed)r.Closed++;else r.CloseFailures++;
    }
    if(r.CloseFailures!=0) { r.Success=false;r.Failure="close"; }
    if(!r.Success) { r.LeafAclVerified=false;r.Status="unknown";r.DescriptorHash=null;r.Leaf=null; }
    clock.Stop();
   }
   return r;
  }
 }
 public sealed class NativeApi : IApi {
  [StructLayout(LayoutKind.Sequential)] struct TimeParts { public uint Low,High; }
  [StructLayout(LayoutKind.Sequential)] struct FileInfo {
   public uint Attributes;public TimeParts Creation,Access,Write;public uint Volume,SizeHigh,SizeLow,Links,IndexHigh,IndexLow;
  }
  [StructLayout(LayoutKind.Sequential)] struct UnicodeName { public ushort Length,MaximumLength;public IntPtr Buffer; }
  [StructLayout(LayoutKind.Sequential)] struct ObjectAttributes {
   public uint Length;public IntPtr RootDirectory,ObjectName;public uint Attributes;public IntPtr SecurityDescriptor,SecurityQualityOfService;
  }
  [StructLayout(LayoutKind.Sequential)] struct IoStatus { public IntPtr Status;public UIntPtr Information; }
  [DllImport("ntdll.dll",ExactSpelling=true)] static extern int NtOpenFile(out IntPtr h,uint access,ref ObjectAttributes attributes,out IoStatus status,uint share,uint options);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true,ExactSpelling=true)] static extern IntPtr CreateFileW(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
  [DllImport("kernel32.dll",SetLastError=true,ExactSpelling=true)] [return:MarshalAs(UnmanagedType.Bool)] static extern bool GetFileInformationByHandle(IntPtr h,out FileInfo info);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true,ExactSpelling=true)] static extern uint GetFinalPathNameByHandleW(IntPtr h,StringBuilder path,uint size,uint flags);
  [DllImport("kernel32.dll",ExactSpelling=true)] static extern uint GetFileType(IntPtr h);
  [DllImport("kernel32.dll",ExactSpelling=true)] [return:MarshalAs(UnmanagedType.Bool)] static extern bool CloseHandle(IntPtr h);
  [DllImport("advapi32.dll",ExactSpelling=true)] static extern uint GetSecurityInfo(IntPtr h,int kind,uint flags,out IntPtr owner,out IntPtr group,out IntPtr dacl,out IntPtr sacl,out IntPtr descriptor);
  [DllImport("advapi32.dll",ExactSpelling=true)] static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
  [DllImport("kernel32.dll",ExactSpelling=true)] static extern IntPtr LocalFree(IntPtr pointer);
  public NativeApi() {
   if(Environment.OSVersion.Platform!=PlatformID.Win32NT || IntPtr.Size!=8 || Marshal.SizeOf(typeof(FileInfo))!=52 ||
      Marshal.SizeOf(typeof(UnicodeName))!=16 || Marshal.SizeOf(typeof(ObjectAttributes))!=48 || Marshal.SizeOf(typeof(IoStatus))!=16)throw new Failure("platform");
  }
  public IntPtr Open(string path,int index,IntPtr parent) {
   return OpenCore(path,index,parent,false);
  }
  internal IntPtr OpenForRestriction(string path,int index,IntPtr parent) {
   return OpenCore(path,index,parent,true);
  }
  IntPtr OpenCore(string path,int index,IntPtr parent,bool restrict) {
   if(index<0 || index>3 || path!=Guard.Paths[index])throw new Failure("component");
   if(index==0) {
    if(parent!=IntPtr.Zero)throw new Failure("root");
    IntPtr root=CreateFileW(path,0x20080,restrict?3u:7u,IntPtr.Zero,3,0x02300000,IntPtr.Zero);
    if(root==IntPtr.Zero || root==new IntPtr(-1))throw new Failure("open");return root;
   }
   if(parent==IntPtr.Zero || parent==new IntPtr(-1))throw new Failure("parent");
   string name=path.Substring(path.LastIndexOf('\\')+1);IntPtr buffer=IntPtr.Zero,unicode=IntPtr.Zero;
   try {
    buffer=Marshal.StringToHGlobalUni(name);var text=new UnicodeName{Length=(ushort)(name.Length*2),MaximumLength=(ushort)(name.Length*2+2),Buffer=buffer};
    unicode=Marshal.AllocHGlobal(16);Marshal.StructureToPtr(text,unicode,false);
    var attributes=new ObjectAttributes{Length=48,RootDirectory=parent,ObjectName=unicode,Attributes=0x1040};
    // Single-component relative open; OBJ_DONT_REPARSE and OPEN_REPARSE_POINT.
    // READ_CONTROL + READ_ATTRIBUTES + SYNCHRONIZE, plus directory listing.
    // Server 2022 rejects FILE_DIRECTORY_FILE combined with these no-recall /
    // no-reparse options. Keep those flags; Validate checks the held parent's
    // directory type before it can be used to open the next component.
    // Restriction adds WRITE_DAC/WRITE_OWNER only to the fixed leaf. Parents
    // exclude delete sharing so their names stay bound while the leaf is held.
    uint access=index==3?(restrict?0x1e0080u:0x120080u):0x120081u;
    IntPtr handle;IoStatus status;int rc=NtOpenFile(out handle,access,ref attributes,out status,index==3?1u:(restrict?3u:7u),0x600020u|(index==3?0x40u:0u));
    if(rc!=0 || handle==IntPtr.Zero || handle==new IntPtr(-1)) {
     if(handle!=IntPtr.Zero && handle!=new IntPtr(-1) && !CloseHandle(handle))throw new Failure("open_close");
     if(index==3 && unchecked((uint)rc)==0xC0000034u)return IntPtr.Zero;
     throw new Failure("open");
    }
    return handle;
   } finally { if(unicode!=IntPtr.Zero)Marshal.FreeHGlobal(unicode);if(buffer!=IntPtr.Zero)Marshal.FreeHGlobal(buffer); }
  }
  public static string CanonicalDescriptor(byte[] bytes) {
   var sd=new RawSecurityDescriptor(bytes,0);
   if(sd.Owner==null || sd.Group==null || sd.DiscretionaryAcl==null || sd.DiscretionaryAcl.Revision!=2 || sd.DiscretionaryAcl.Count>64)throw new Failure("acl_form");
   var result=new StringBuilder(((int)sd.ControlFlags).ToString(CultureInfo.InvariantCulture)+"|"+sd.Owner.Value+"|"+sd.Group.Value+"|");
   foreach(GenericAce entry in sd.DiscretionaryAcl) {
    var ace=entry as CommonAce;
    if(ace==null || ace.IsCallback || ace.AceQualifier!=AceQualifier.AccessAllowed)throw new Failure("acl_form");
    result.Append("0,").Append(((int)ace.AceFlags).ToString(CultureInfo.InvariantCulture)).Append(',')
     .Append(unchecked((uint)ace.AccessMask).ToString(CultureInfo.InvariantCulture)).Append(',').Append(ace.SecurityIdentifier.Value).Append(';');
   }
   if(result.Length>8192)throw new Failure("acl_bound");return result.ToString();
  }
  static byte[] Descriptor(IntPtr h) {
   IntPtr owner,group,dacl,sacl,descriptor;
   // Only OWNER/GROUP/DACL are queried. No SACL claim or privilege adjustment.
   if(GetSecurityInfo(h,1,7,out owner,out group,out dacl,out sacl,out descriptor)!=0)throw new Failure("acl_query");
   try {
    if(descriptor==IntPtr.Zero || dacl==IntPtr.Zero)throw new Failure("acl_query");
    uint length=GetSecurityDescriptorLength(descriptor);if(length<20 || length>8192)throw new Failure("acl_bound");
    var raw=new byte[length];Marshal.Copy(descriptor,raw,0,(int)length);return raw;
   } finally { if(descriptor!=IntPtr.Zero && LocalFree(descriptor)!=IntPtr.Zero)throw new Failure("acl_close"); }
  }
  public Metadata Info(IntPtr h,int index) {
   FileInfo f;if(!GetFileInformationByHandle(h,out f))throw new Failure("metadata");
   var path=new StringBuilder(512);uint n=GetFinalPathNameByHandleW(h,path,512,0);
   if(n==0 || n>=512)throw new Failure("path_bound");string value=path.ToString();
   if(!value.StartsWith(@"\\?\",StringComparison.Ordinal))throw new Failure("path_prefix");
   byte[] sd=Descriptor(h);string digest;
   using(var hash=SHA256.Create()) { digest=BitConverter.ToString(hash.ComputeHash(sd)).Replace("-","").ToLowerInvariant(); }
   return new Metadata{Kind=GetFileType(h),Volume=f.Volume,FileId=((ulong)f.IndexHigh<<32)|f.IndexLow,Attributes=f.Attributes,Links=f.Links,
     Size=((ulong)f.SizeHigh<<32)|f.SizeLow,Creation=((ulong)f.Creation.High<<32)|f.Creation.Low,Write=((ulong)f.Write.High<<32)|f.Write.Low,
     Path=value.Substring(4),Security=CanonicalDescriptor(sd),DescriptorHash=digest};
  }
  public bool Close(IntPtr h) { return CloseHandle(h); }
 }
}
