import React, { useState } from 'react';
import {
    Dialog,
    DialogTitle,
    DialogContent,
    DialogActions,
    Button,
    Table,
    TableBody,
    TableCell,
    TableContainer,
    TableHead,
    TableRow,
    Paper,
    IconButton,
    Box,
    Typography,
} from '@mui/material';
import InfoIcon from '@mui/icons-material/Info';
import DeleteIcon from '@mui/icons-material/Delete';
import ErrorIcon from '@mui/icons-material/Error';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import { useDispatch } from 'react-redux';
import { 
    getData, 
    setOpenNewReport, 
    setShowCasesList,
    getJsonFromXml,
    setShowUpload
} from '@src/features/display/slice';
import { getMeddraReleases } from '@src/features/meddra/slice';

export const DataTable = ({
    open,
    onClose,
    itemsList,
    onAction,
    onExclude,
    title = "Data Table",
    actionButtonLabel = "Action",
    canOpenItems = true,
    showIdColumn = true
}) => {
    const dispatch = useDispatch();
    const [infoDialogOpen, setInfoDialogOpen] = useState(false);
    const [currentInfoItem, setCurrentInfoItem] = useState(null);

    const openInfoDialog = (itemId) => {
        setCurrentInfoItem(itemId);
        setInfoDialogOpen(true);
    };
    
    const closeInfoDialog = () => {
        setInfoDialogOpen(false);
    };

    const openReport = (id) => {
        if (id) {
            dispatch(getData(id));
            dispatch(setOpenNewReport(true));
            dispatch(getMeddraReleases());
            dispatch(setShowCasesList(false));
            onClose();
        }
    };

    const openCiomsReport = (id) => {
        if (id) {
            window.open(`/api/api/cioms/${id}`, '_blank');
        }
    };

    return (
        <>
            {}
            <Dialog
                open={open}
                onClose={onClose}
                maxWidth="md"
                fullWidth
            >
                <DialogTitle>{title}</DialogTitle>
                <DialogContent>
                    <TableContainer component={Paper} sx={{ mt: 2 }}>
                        <Table>
                            <TableHead>
                                <TableRow>
                                    {canOpenItems && <TableCell>Open</TableCell>}
                                    {showIdColumn && <TableCell>ID</TableCell>}
                                    <TableCell>Name</TableCell>
                                    <TableCell>Status</TableCell>
                                    <TableCell>Info</TableCell>
                                    <TableCell>Exclude</TableCell>
                                </TableRow>
                            </TableHead>
                            <TableBody>
                                {itemsList && itemsList.map((item) => (
                                    <TableRow key={item.id || item.tempId || Math.random().toString()}>
                                        {}
                                        {canOpenItems && (
                                            <TableCell>
                                                <Button
                                                    variant="contained"
                                                    size="small"
                                                    onClick={() => openReport(item.id)}
                                                    className="myBtOpen"
                                                    disabled={!item.id}
                                                    sx={{
                                                        backgroundColor: '#1976d2',
                                                        color: 'white',
                                                        '&:hover': {
                                                            backgroundColor: '#1565c0',
                                                        }
                                                    }}
                                                >
                                                    OPEN
                                                </Button>
                                            </TableCell>
                                        )}
                                        
                                        {}
                                        {showIdColumn && (
                                            <TableCell>
                                                {item.id ? (
                                                    <Button 
                                                        variant="text" 
                                                        color="primary"
                                                        onClick={() => openCiomsReport(item.id)}
                                                    >
                                                        {item.display_id || item.id || "null"}
                                                    </Button>
                                                ) : (
                                                    <Typography variant="body2" color="textSecondary">
                                                        null
                                                    </Typography>
                                                )}
                                            </TableCell>
                                        )}
                                        
                                        {}
                                        <TableCell>
                                            {item.name || item.fileName || item.display_id || ""}
                                        </TableCell>
                                        
                                        {}
                                        <TableCell>
                                            <Box sx={{ display: 'flex', alignItems: 'center', color: item.isValid ? 'success.main' : 'error.main' }}>
                                                {item.isValid ? (
                                                    <>
                                                        <CheckCircleIcon sx={{ mr: 1 }} />
                                                        Valid
                                                    </>
                                                ) : (
                                                    <>
                                                        <ErrorIcon sx={{ mr: 1 }} />
                                                        Invalid
                                                    </>
                                                )}
                                            </Box>
                                        </TableCell>
                                        
                                        {}
                                        <TableCell>
                                            <IconButton 
                                                color="primary" 
                                                onClick={() => openInfoDialog(item.id || item.tempId)}
                                            >
                                                <InfoIcon />
                                            </IconButton>
                                        </TableCell>
                                        
                                        {}
                                        <TableCell>
                                            <IconButton 
                                                color="error" 
                                                onClick={() => onExclude(item.id || item.tempId)}
                                            >
                                                <DeleteIcon />
                                            </IconButton>
                                        </TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </TableContainer>
                </DialogContent>
                <DialogActions>
                    <Button variant="outlined" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button 
                        variant="contained" 
                        color="primary" 
                        onClick={() => onAction(itemsList)}
                        disabled={!itemsList || itemsList.length === 0}
                    >
                        {actionButtonLabel}
                    </Button>
                </DialogActions>
            </Dialog>
            
            {}
            <Dialog
                open={infoDialogOpen}
                onClose={closeInfoDialog}
                maxWidth="sm"
            >
                <DialogTitle>Validation Information</DialogTitle>
                <DialogContent>
                    {currentInfoItem && (
                        <>
                            <Typography variant="body1" gutterBottom>
                                Required fields that need to be filled:
                            </Typography>
                            <Paper 
                                elevation={0} 
                                sx={{ 
                                    bgcolor: '#f5f5f5', 
                                    p: 2, 
                                    mt: 2, 
                                    maxHeight: '200px', 
                                    overflow: 'auto' 
                                }}
                            >
                                {itemsList && itemsList.find(item => (item.id || item.tempId) === currentInfoItem)?.missingFields?.map((field, index) => (
                                    <Typography key={index} variant="body2" gutterBottom sx={{ pl: 2 }}>
                                        • {field.label}: {field.description} (C.1.1 = {field.externalKey})
                                    </Typography>
                                )) || (
                                    <Typography variant="body2">
                                        No detailed information available.
                                    </Typography>
                                )}
                            </Paper>
                        </>
                    )}
                </DialogContent>
                <DialogActions>
                    <Button variant="outlined" onClick={closeInfoDialog}>
                        Back
                    </Button>
                </DialogActions>
            </Dialog>
        </>
    );
};
