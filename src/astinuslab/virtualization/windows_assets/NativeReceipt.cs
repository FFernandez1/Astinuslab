using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Text;
using System.Text.RegularExpressions;
namespace UnattendedReceipt {
 public sealed class Failure : Exception {public string Code; public Failure(string code){Code=code;}}
 public sealed class Metadata {
  public uint Attributes,Links,Volume,Kind; public ulong FileId,Size,Creation,Write;
  public string Path; public bool AclSafe;
 }
 public interface IApi {
  IntPtr Open(string path,int index,IntPtr parent); Metadata Info(IntPtr handle,int index);
  byte[] Read(IntPtr handle,int count); bool Close(IntPtr handle);
 }
 public sealed class Result {
  public bool Success,Enabled; public string PasswordLastSetTicks;
  public string Failure="none"; public int Opened,Closed,CloseFailures;
 }
 public static class Guard {
  public const string Nonce="__ASTINUS_NONCE__";
  static readonly string Root=@"C:\ProgramData\AstinusLabSetup-"+Nonce;
  static readonly string[] Paths={@"C:\",@"C:\ProgramData",Root,Root+@"\attempt.json",Root+@"\initialized.json"};
  static void Check(bool value,string code){if(!value)throw new Failure(code);}
  static void Validate(Metadata m,int i,uint volume){
   Check(m!=null && m.Kind==1,"kind");Check((m.Attributes&1024)==0,"reparse");
   Check((m.Attributes&(0x1000u|0x40000u|0x400000u))==0,"offline_or_recall");
   Check(((m.Attributes&16)!=0)==(i<3),"type");
   Check(String.Equals(m.Path,Paths[i],StringComparison.OrdinalIgnoreCase),"path");
   Check(m.Volume==volume && m.FileId!=0,"identity");
   if(i>=2)Check(m.AclSafe,"acl");
   if(i>=3)Check(m.Links==1,"links");
   if(i==4)Check(m.Size>0 && m.Size<=1024,"size");
  }
  static void Resample(IApi api,List<IntPtr> handles,List<Metadata> initial,uint volume,Stopwatch clock){
   for(int i=0;i<handles.Count;i++){
    Check(clock.ElapsedMilliseconds<5000,"deadline");var m=api.Info(handles[i],i);Validate(m,i,volume);
    Check(m.FileId==initial[i].FileId && m.Attributes==initial[i].Attributes,"changed");
    if(i>=2)Check(m.Creation==initial[i].Creation && m.Write==initial[i].Write,"changed");
    if(i>=3)Check(m.Links==initial[i].Links && m.Size==initial[i].Size,"changed");
   }
  }
  public static Result CollectNative(){
   try{return Collect(new NativeApi());}catch(Failure e){return new Result{Failure=e.Code};}
   catch(Exception){return new Result{Failure="unexpected"};}
  }
  public static Result Collect(IApi api){
   var r=new Result();var hs=new List<IntPtr>();var initial=new List<Metadata>();
   var seen=new HashSet<ulong>();var clock=Stopwatch.StartNew();byte[] bytes=null;uint volume=0;
   try{
    for(int i=0;i<Paths.Length;i++){
     Check(clock.ElapsedMilliseconds<5000,"deadline");IntPtr parent=i==0?IntPtr.Zero:hs[i>=3?2:i-1];
     IntPtr h=api.Open(Paths[i],i,parent);
     Check(h!=IntPtr.Zero && h!=new IntPtr(-1),"open");hs.Add(h);r.Opened++;
     var m=api.Info(h,i);if(i==0){Check(m!=null,"metadata");volume=m.Volume;}
     Validate(m,i,volume);Check(seen.Add(m.FileId),"alias");initial.Add(m);
    }
    Resample(api,hs,initial,volume,clock);
    bytes=api.Read(hs[4],checked((int)initial[4].Size));
    Check(bytes!=null && bytes.Length==(int)initial[4].Size,"read_size");
    Resample(api,hs,initial,volume,clock);
    string text=new UTF8Encoding(false,true).GetString(bytes);
    string pattern="\\A\\{\"schema_version\":1,\"nonce\":\""+Nonce+"\",\"stage\":\"specialize\",\"initialized\":true,\"enabled\":(true|false),\"password_last_set_ticks\":\"([1-9][0-9]{0,18})\"\\}\\z";
    var match=Regex.Match(text,pattern,RegexOptions.CultureInvariant,TimeSpan.FromSeconds(1));
    Check(match.Success,"receipt_format");ulong ticks;
    Check(UInt64.TryParse(match.Groups[2].Value,NumberStyles.None,CultureInfo.InvariantCulture,out ticks) && ticks<=3155378975999999999UL,"receipt_ticks");
    r.Enabled=match.Groups[1].Value=="true";r.PasswordLastSetTicks=match.Groups[2].Value;r.Success=true;
   }catch(Failure e){r.Failure=e.Code;}catch(Exception){r.Failure="unexpected";}
   finally{
    if(bytes!=null)Array.Clear(bytes,0,bytes.Length);
    for(int i=hs.Count-1;i>=0;i--){bool closed=false;try{closed=api.Close(hs[i]);}catch(Exception){}
     if(closed)r.Closed++;else r.CloseFailures++;
    }
    if(r.CloseFailures!=0){r.Success=false;r.Failure="close";}
    if(!r.Success){r.Enabled=false;r.PasswordLastSetTicks=null;}
    clock.Stop();
   }
   return r;
  }
 }
 public sealed class NativeApi : IApi {
  [StructLayout(LayoutKind.Sequential)] struct TimeParts{public uint Low,High;}
  [StructLayout(LayoutKind.Sequential)] struct FileInfo{
   public uint Attributes;public TimeParts Creation,Access,Write;
   public uint Volume,SizeHigh,SizeLow,Links,IndexHigh,IndexLow;
  }
  [StructLayout(LayoutKind.Sequential)] struct UnicodeName{public ushort Length,MaximumLength;public IntPtr Buffer;}
  [StructLayout(LayoutKind.Sequential)] struct ObjectAttributes{public uint Length;public IntPtr RootDirectory,ObjectName;public uint Attributes;public IntPtr SecurityDescriptor,SecurityQualityOfService;}
  [StructLayout(LayoutKind.Sequential)] struct IoStatus{public IntPtr Status;public UIntPtr Information;}
  [DllImport("ntdll.dll",ExactSpelling=true)] static extern int NtOpenFile(out IntPtr handle,uint access,ref ObjectAttributes attributes,out IoStatus status,uint share,uint options);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true,ExactSpelling=true)]
  static extern IntPtr CreateFileW(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
  [DllImport("kernel32.dll",SetLastError=true,ExactSpelling=true)]
  [return:MarshalAs(UnmanagedType.Bool)] static extern bool GetFileInformationByHandle(IntPtr h,out FileInfo info);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true,ExactSpelling=true)]
  static extern uint GetFinalPathNameByHandleW(IntPtr h,StringBuilder path,uint size,uint flags);
  [DllImport("kernel32.dll",ExactSpelling=true)] static extern uint GetFileType(IntPtr h);
  [DllImport("kernel32.dll",SetLastError=true,ExactSpelling=true)]
  [return:MarshalAs(UnmanagedType.Bool)] static extern bool ReadFile(IntPtr h,byte[] bytes,uint count,out uint got,IntPtr overlapped);
  [DllImport("kernel32.dll",ExactSpelling=true)] [return:MarshalAs(UnmanagedType.Bool)] static extern bool CloseHandle(IntPtr h);
  [DllImport("advapi32.dll",ExactSpelling=true)] static extern uint GetSecurityInfo(IntPtr h,int kind,uint flags,out IntPtr owner,out IntPtr group,out IntPtr dacl,out IntPtr sacl,out IntPtr descriptor);
  [DllImport("advapi32.dll",ExactSpelling=true)] static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
  [DllImport("kernel32.dll",ExactSpelling=true)] static extern IntPtr LocalFree(IntPtr pointer);
  public NativeApi(){
   if(Environment.OSVersion.Platform!=PlatformID.Win32NT || IntPtr.Size!=8 || Marshal.SizeOf(typeof(FileInfo))!=52 ||
      Marshal.SizeOf(typeof(UnicodeName))!=16 || Marshal.SizeOf(typeof(ObjectAttributes))!=48 || Marshal.SizeOf(typeof(IoStatus))!=16)throw new Failure("platform");
  }
  public IntPtr Open(string path,int index,IntPtr parent){
   if(index==0){
    if(path!=@"C:\" || parent!=IntPtr.Zero)throw new Failure("root");
    IntPtr root=CreateFileW(path,0x81,7,IntPtr.Zero,3,0x02300000,IntPtr.Zero);
    if(root==IntPtr.Zero || root==new IntPtr(-1))throw new Failure("open");return root;
   }
   if(index<1 || index>4 || parent==IntPtr.Zero)throw new Failure("parent");
   string name=path.Substring(path.LastIndexOf('\\')+1);
   if(name.Length<1 || name.Length>128 || name.IndexOf(':')>=0 || name=="." || name=="..")throw new Failure("component");
   IntPtr buffer=IntPtr.Zero;IntPtr unicode=IntPtr.Zero;
   try{
    buffer=Marshal.StringToHGlobalUni(name);
    var text=new UnicodeName{Length=(ushort)(name.Length*2),MaximumLength=(ushort)(name.Length*2+2),Buffer=buffer};
    unicode=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeName)));Marshal.StructureToPtr(text,unicode,false);
    var attributes=new ObjectAttributes{Length=48,RootDirectory=parent,ObjectName=unicode,Attributes=0x1040};
    // Relative single-component open; OBJ_DONT_REPARSE|OBJ_CASE_INSENSITIVE.
    // Synchronous, no recall, open reparse itself; never create or truncate.
    uint options=0x600020u|(index<3?0u:0x40u);
    uint access=index>=3?0x80100080u:(index==2?0x120081u:0x100081u);
    IntPtr handle;IoStatus status;int result=NtOpenFile(out handle,access,ref attributes,out status,index<2?7u:1u,options);
    if(result!=0 || handle==IntPtr.Zero || handle==new IntPtr(-1)){
     if(handle!=IntPtr.Zero && handle!=new IntPtr(-1) && !CloseHandle(handle))throw new Failure("open_close");
     throw new Failure("open");
    }
    return handle;
   }finally{if(unicode!=IntPtr.Zero)Marshal.FreeHGlobal(unicode);if(buffer!=IntPtr.Zero)Marshal.FreeHGlobal(buffer);}
  }
  bool RestrictedAcl(IntPtr h,bool directory){
   IntPtr owner,group,dacl,sacl,descriptor;
   uint rc=GetSecurityInfo(h,1,5,out owner,out group,out dacl,out sacl,out descriptor);
   if(rc!=0)throw new Failure("acl_query");
   try{
    if(descriptor==IntPtr.Zero || dacl==IntPtr.Zero)return false;
    uint length=GetSecurityDescriptorLength(descriptor);if(length==0 || length>8192)return false;
    byte[] raw=new byte[length];Marshal.Copy(descriptor,raw,0,(int)length);
    var sd=new RawSecurityDescriptor(raw,0);
    if((sd.ControlFlags&ControlFlags.DiscretionaryAclPresent)==0 || (sd.ControlFlags&ControlFlags.DiscretionaryAclDefaulted)!=0)return false;
    if(sd.Owner==null || (sd.Owner.Value!="S-1-5-18" && (directory || sd.Owner.Value!="S-1-5-32-544")))return false;
    if(sd.DiscretionaryAcl==null || sd.DiscretionaryAcl.Count!=2)return false;
    if(directory && (sd.ControlFlags&ControlFlags.DiscretionaryAclProtected)==0)return false;
    var sids=new HashSet<string>();
    foreach(GenericAce entry in sd.DiscretionaryAcl){
     var ace=entry as CommonAce;if(ace==null || ace.IsCallback || ace.AceQualifier!=AceQualifier.AccessAllowed || ace.AccessMask!=0x1F01FF)return false;
     if(ace.AceFlags!=(directory?(AceFlags.ObjectInherit|AceFlags.ContainerInherit):AceFlags.Inherited))return false;
     string sid=ace.SecurityIdentifier.Value;if(sid!="S-1-5-18" && sid!="S-1-5-32-544")return false;if(!sids.Add(sid))return false;
    }
    return sids.Count==2;
   }finally{if(descriptor!=IntPtr.Zero && LocalFree(descriptor)!=IntPtr.Zero)throw new Failure("acl_close");}
  }
  public Metadata Info(IntPtr h,int index){
   FileInfo f;if(!GetFileInformationByHandle(h,out f))throw new Failure("metadata");
   var path=new StringBuilder(512);uint n=GetFinalPathNameByHandleW(h,path,512,0);
   if(n==0 || n>=512)throw new Failure("path_bound");string value=path.ToString();
   if(!value.StartsWith(@"\\?\",StringComparison.Ordinal))throw new Failure("path_prefix");
   return new Metadata{Attributes=f.Attributes,Links=f.Links,Volume=f.Volume,Kind=GetFileType(h),
    FileId=((ulong)f.IndexHigh<<32)|f.IndexLow,Size=((ulong)f.SizeHigh<<32)|f.SizeLow,
    Creation=((ulong)f.Creation.High<<32)|f.Creation.Low,Write=((ulong)f.Write.High<<32)|f.Write.Low,
    Path=value.Substring(4),AclSafe=index<2 || RestrictedAcl(h,index==2)};
  }
  public byte[] Read(IntPtr h,int count){
   if(count<1 || count>1024)throw new Failure("size");byte[] bytes=new byte[count];uint got;
   if(!ReadFile(h,bytes,(uint)count,out got,IntPtr.Zero) || got!=(uint)count){Array.Clear(bytes,0,bytes.Length);throw new Failure("read");}
   var extra=new byte[1];bool eof=ReadFile(h,extra,1,out got,IntPtr.Zero) && got==0;Array.Clear(extra,0,1);
   if(!eof){Array.Clear(bytes,0,bytes.Length);throw new Failure("eof");}
   return bytes;
  }
  public bool Close(IntPtr h){return CloseHandle(h);}
 }
}
