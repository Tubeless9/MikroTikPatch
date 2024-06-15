import subprocess,lzma
import struct,os
from npk import NovaPackage,NpkPartID,NpkFileContainer

def patch_bzimage(data:bytes,key_dict:dict):
    print(f'bzImage size : {len(data)}')
    PE_TEXT_SECTION_OFFSET = 414
    HEADER_PAYLOAD_OFFSET = 584
    HEADER_PAYLOAD_LENGTH_OFFSET = HEADER_PAYLOAD_OFFSET + 4
    text_section_raw_data = struct.unpack_from('<I',data,PE_TEXT_SECTION_OFFSET)[0]
    payload_offset =  text_section_raw_data +struct.unpack_from('<I',data,HEADER_PAYLOAD_OFFSET)[0]
    payload_length = struct.unpack_from('<I',data,HEADER_PAYLOAD_LENGTH_OFFSET)[0]
    payload_length = payload_length - 4 #last 4 bytes is uncompressed size(z_output_len)
    print(f'vmlinux xz offset : {payload_offset}')
    print(f'vmlinux xz size : {payload_length}')
    z_output_len = struct.unpack_from('<I',data,payload_offset+payload_length)[0]
    print(f'z_output_len : {z_output_len}')
    vmlinux_xz = data[payload_offset:payload_offset+payload_length]
    vmlinux = lzma.decompress(vmlinux_xz)
    print(f'vmlinux size : {len(vmlinux)}')
    assert z_output_len == len(vmlinux), 'vmlinux size is not equal to expected'
    CPIO_HEADER_MAGIC = b'07070100'
    CPIO_FOOTER_MAGIC = b'TRAILER!!!\x00\x00\x00\x00' #545241494C455221212100000000
    cpio_offset1 = vmlinux.index(CPIO_HEADER_MAGIC)
    initramfs = vmlinux[cpio_offset1:]
    cpio_offset2 = initramfs.index(CPIO_FOOTER_MAGIC)+len(CPIO_FOOTER_MAGIC)
    initramfs = initramfs[:cpio_offset2]
    new_initramfs = initramfs       
    for old_public_key,new_public_key in key_dict.items():
        if old_public_key in new_initramfs:
            print(f'initramfs public key patched {old_public_key[:16].hex().upper()}...')
            new_initramfs = new_initramfs.replace(old_public_key,new_public_key)
    new_vmlinux = vmlinux.replace(initramfs,new_initramfs)
    new_vmlinux_xz = lzma.compress(new_vmlinux,check=lzma.CHECK_CRC32,filters=[
            {"id": lzma.FILTER_X86},
            {"id": lzma.FILTER_LZMA2, "preset": 8,'dict_size': 32*1024*1024},
        ])
    new_payload_length = len(new_vmlinux_xz)
    print(f'new vmlinux xz size : {new_payload_length}')
    assert new_payload_length <= payload_length , 'new vmlinux.xz size is too big'
    new_payload_length = new_payload_length + 4 #last 4 bytes is uncompressed size(z_output_len)
    new_data = bytearray(data)
    struct.pack_into('<I',new_data,HEADER_PAYLOAD_LENGTH_OFFSET,new_payload_length)
    vmlinux_xz += struct.pack('<I',z_output_len)
    new_vmlinux_xz += struct.pack('<I',z_output_len)
    new_vmlinux_xz = new_vmlinux_xz.ljust(len(vmlinux_xz),b'\0')
    new_data = new_data.replace(vmlinux_xz,new_vmlinux_xz)
    print(f'new bzImage size : {len(new_data)}')
    return new_data

def run_shell_command(command):
    process = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return process.stdout, process.stderr

def patch_squashfs(path,key_dict):
    for root, dirs, files in os.walk(path):
        for file in files:
            file = os.path.join(root,file)
            if os.path.isfile(file):
                data = open(file,'rb').read()
                for old_public_key,new_public_key in key_dict.items():
                    if old_public_key in data:
                        print(f'{file} public key patched {old_public_key[:16].hex().upper()}...')
                        data = data.replace(old_public_key,new_public_key)
                        open(file,'wb').write(data)

def patch_system_npk(npk_file,key_dict):
    npk = NovaPackage.load(npk_file)
    file_container = NpkFileContainer.unserialize_from(npk[NpkPartID.FILE_CONTAINER].data)
    for item in file_container:
        if item.name == b'boot/EFI/BOOT/BOOTX64.EFI':
            print(f'patch {item.name} ...')
            item.data = patch_bzimage(item.data,key_dict)
            open('linux','wb').write(item.data)
            break
    npk[NpkPartID.FILE_CONTAINER].data = file_container.serialize()
    try:
        squashfs_file = 'squashfs.sfs'
        extract_dir = 'squashfs-root'
        open(squashfs_file,'wb').write(npk[NpkPartID.SQUASHFS].data)
        print(f"extract {squashfs_file} ...")
        _, stderr = run_shell_command(f"unsquashfs -d {extract_dir} {squashfs_file}")
        print(stderr.decode())
        patch_squashfs(extract_dir,key_dict)
        print(f"pack {extract_dir} ...")
        run_shell_command(f"rm -f {squashfs_file}")
        _, stderr = run_shell_command(f"mksquashfs {extract_dir} {squashfs_file} -quiet -comp xz -no-xattrs -b 256k")
        print(stderr.decode())
    except Exception as e:
        print(e)
    print(f"clean ...")
    run_shell_command(f"rm -rf {extract_dir}")
    npk[NpkPartID.SQUASHFS].data = open(squashfs_file,'rb').read()
    run_shell_command(f"rm -f {squashfs_file}")
    kcdsa_private_key = bytes.fromhex(os.environ['CUSTOM_LICENSE_PRIVATE_KEY'])
    eddsa_private_key = bytes.fromhex(os.environ['CUSTOM_NPK_SIGN_PRIVATE_KEY'])
    npk.sign(kcdsa_private_key,eddsa_private_key)
    npk.save(npk_file)

if __name__ == '__main__':
    import os,sys
    key_dict = {
        bytes.fromhex(os.environ['MIKRO_LICENSE_PUBLIC_KEY']):bytes.fromhex(os.environ['CUSTOM_LICENSE_PUBLIC_KEY']),
        bytes.fromhex(os.environ['MIKRO_NPK_SIGN_PUBLIC_LKEY']):bytes.fromhex(os.environ['CUSTOM_NPK_SIGN_PUBLIC_KEY'])
    }
    if len(sys.argv) == 2:
        print(f'patching {sys.argv[1]} ...')
        patch_system_npk(sys.argv[1],key_dict)
    else:
        print('usage: python patch.py npk_file')
    