from macho_definitions import *
import macho_load_commands


class MachoBinary(object):
    def __init__(self, fat_file, offset_within_fat=0):
        # type: (file, int) -> MachoBinary
        self._file = fat_file
        self.offset_within_fat = offset_within_fat

        self.is_64bit = False
        self.load_commands_offset = 0
        self._num_commands = 0
        self.cpu_type = CPU_TYPE.UNKNOWN
        self.magic = 0
        self.is_swap = False

        self.header = None
        self.segments = {}
        self.sections = {}
        self.dysymtab = None
        self.symtab = None
        self.encryption_info = None

        self.parse()

    def parse(self):
        """
        Attempt to parse the provided file contents as a MachO slice
        This method may throw an exception if the provided data does not represent a valid or supported
        Mach-O slice.
        """
        if not self.check_magic():
            return
        self.is_swap = self.should_swap_bytes()
        self.is_64bit = self.magic_is_64()
        self.parse_header()

    def check_magic(self):
        """
        Ensure magic at provided offset within provided file represents a valid and supported Mach-O slice
        Sets up byte swapping if host and slice differ in endianness
        This method will throw an exception if the magic is invalid or unsupported
        Returns:
            True if the magic represents a supported file format
        """
        self.magic = c_uint32.from_buffer(bytearray(self.get_bytes(0, sizeof(c_uint32)))).value
        valid_mag = [
            MachArch.MH_MAGIC_64,
            MachArch.MH_CIGAM_64
        ]
        mag32 = [
            MachArch.MH_MAGIC,
            MachArch.MH_CIGAM,
        ]
        self.is_swap = self.should_swap_bytes()

        if self.magic in mag32:
            raise RuntimeError('32-bit Mach-O slices not supported')
        if self.magic not in valid_mag:
            raise RuntimeError('Macho slice @ {} had invalid magic {}'.format(
                hex(int(self.offset_within_fat)),
                hex(int(self.magic))
            ))
        return True

    def magic_is_64(self):
        """
        Convenience method to check if our magic corresponds to a 64-bit slice
        Returns:
            True if self.magic corresponds to a 64 bit MachO slice, False otherwise
        """
        return self.magic == MachArch.MH_MAGIC_64 or self.magic == MachArch.MH_CIGAM_64

    def parse_header(self):
        """
        Parse all info from a Mach-O header.
        This method will also parse all segment and section commands.
        """
        header_bytes = self.get_bytes(0, sizeof(MachoHeader64))
        self.header = MachoHeader64.from_buffer(bytearray(header_bytes))

        if self.header.cputype == MachArch.MH_CPU_TYPE_ARM:
            self.cpu_type = CPU_TYPE.ARMV7
        elif self.header.cputype == MachArch.MH_CPU_TYPE_ARM64:
            self.cpu_type = CPU_TYPE.ARM64
        else:
            self.cpu_type = CPU_TYPE.UNKNOWN

        self._num_commands = self.header.ncmds
        # load commands begin directly after Mach O header
        self.load_commands_offset = sizeof(MachoHeader64)
        self.parse_segment_commands(self.load_commands_offset)

    def parse_segment_commands(self, offset):
        """
        Parse Mach-O segment commands beginning at a given slice offset
        Args:
            offset: Slice offset to first segment command
        """
        for i in range(self._num_commands):
            load_command_bytes = self.get_bytes(offset, sizeof(MachOLoadCommand))
            load_command = MachOLoadCommand.from_buffer(bytearray(load_command_bytes))
            # TODO(pt) handle byte swap of load_command
            if load_command.cmd == MachoLoadCommands.LC_SEGMENT:
                # 32 bit segments unsupported!
                continue

            # some commands have their own structure that we interpret seperately from a normal load command
            # if we want to interpret more commands in the future, this is the place to do it
            if load_command.cmd == MachoLoadCommands.LC_SYMTAB:
                symtab_bytes = self.get_bytes(offset, sizeof(MachoSymtabCommand))
                self.symtab = MachoSymtabCommand.from_buffer(bytearray(symtab_bytes))

            elif load_command.cmd == MachoLoadCommands.LC_DYSYMTAB:
                dysymtab_bytes = self.get_bytes(offset, sizeof(MachoDysymtabCommand))
                self.dysymtab = MachoDysymtabCommand.from_buffer(bytearray(dysymtab_bytes))

            elif load_command.cmd == MachoLoadCommands.LC_ENCRYPTION_INFO_64:
                encryption_info_bytes = self.get_bytes(offset, sizeof(MachoEncryptionInfo64Command))
                self.encryption_info = MachoEncryptionInfo64Command.from_buffer(bytearray(encryption_info_bytes))

            elif load_command.cmd == MachoLoadCommands.LC_SEGMENT_64:
                segment_bytes = self.get_bytes(offset, sizeof(MachoSegmentCommand64))
                segment = MachoSegmentCommand64.from_buffer(bytearray(segment_bytes))
                # TODO(pt) handle byte swap of segment
                self.segments[segment.segname] = segment
                self.parse_sections(segment, offset)

            # move to next load command in header
            offset += load_command.cmdsize

    def parse_sections(self, segment, segment_offset):
        # type: (MachoSegmentCommand64, int) -> None
        """
        Parse all sections contained within a Mach-O segment,
        and add them to our map of sections
        Args:
            segment: The segment command whose sections should be read
            segment_offset: The offset within the file that the segment command is located at
        """
        if not segment.nsects:
            return

        # the first section of this segment begins directly after the segment
        section_offset = segment_offset + sizeof(MachoSegmentCommand64)
        section_size = sizeof(MachoSection64)

        for i in range(segment.nsects):
            section_bytes = self.get_bytes(section_offset, sizeof(MachoSection64))
            section = MachoSection64.from_buffer(bytearray(section_bytes))
            # TODO(pt) handle byte swap of segment
            # add to our section with the section name as the key
            self.sections[section.sectname] = section

            section_offset += section_size

    def get_section_with_name(self, name):
        # type: (str) -> Optional[MachoSection64]
        """
        Convenience method to retrieve a section with a given name from map
        Args:
            name: The name of the section to find
        Returns:
            The MachoSection64 command if it is found, None otherwise
        """
        if name in self.sections:
            return self.sections[name]
        return None

    def get_section_content(self, section):
        # type: (MachoSection64) -> bytearray
        """
        Convenience method to retrieve slice content associated with a section command
        Args:
            section: The section command whose corresponding content should be found
        Returns:
            bytearray containing the file contents associated with the section command
        """
        return bytearray(self.get_bytes(section.offset, section.size))

    def get_virtual_base(self):
        # type: (None) -> (int)
        """
        Retrieve the first virtual address of the Mach-O slice
        Returns:
            int containing the virtual memory space address that the Mach-O slice requests to begin at
        """
        text_seg = self.segments['__TEXT']
        return text_seg.vmaddr

    def get_bytes(self, offset, size):
        # type: (int, int) -> str
        """
        Retrieve bytes from Mach-O slice, taking into account that the slice could be at an offset within a FAT
        Args:
            offset: index from beginning of slice to retrieve data from
            size: maximum number of bytes to read

        Returns:
            string containing byte content of mach-o slice at an offset from the start of the slice
        """
        # ensure file is open
        self._file = open(self._file.name)
        # account for the fact that this Macho slice is not necessarily the start of the file!
        # add slide to our macho slice to file seek
        self._file.seek(offset + self.offset_within_fat)
        content = self._file.read(size)

        # now that we've read our data, close file again
        self._file.close()
        return content

    def should_swap_bytes(self):
        # type: (None) -> bool
        """
        Check whether self.magic refers to a big-endian Mach-O binary
        Returns:
            True if self.magic indicates a big endian Mach-O, False otherwise
        """
        # TODO(pt): figure out whether we need to swap to little or big endian,
        # based on system endianness and binary endianness
        # everything we touch currently is little endian, so let's not worry about it for now
        big_endian = [MachArch.MH_CIGAM,
                      MachArch.MH_CIGAM_64,
                      ]
        return self.magic in big_endian